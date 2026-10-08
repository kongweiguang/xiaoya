"""显式连接私有服务，以合成中文语音验收音频房间；Live2D 另在浏览器中验收。"""

import argparse
import asyncio
import io
import json
import os
import sys
import time
import uuid
import wave
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc
from verification_room import create_verification_resources, verification_cleanup

from xiaoya.infrastructure.settings import Settings, validate_credentials


async def wait_until(predicate: Callable[[], bool], timeout: float = 60) -> None:
    """每一步单独限时，失败保留证据且始终进入房间清理。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("房间验收阶段超时")
        await asyncio.sleep(0.05)


async def publish_wav(source: rtc.AudioSource, content: bytes) -> int:
    """按实时麦克风速率发送合成音频，补充静音让本地 VAD 检测轮次结束。"""
    with wave.open(io.BytesIO(content)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("验收合成音频必须是单声道 16 位 PCM WAV")
        rate = wav.getframerate()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    if not len(pcm):
        raise ValueError("验收合成音频不能为空")
    count = round(len(pcm) * 48000 / rate)
    speech = np.interp(np.arange(count) * rate / 48000, np.arange(len(pcm)), pcm)
    output = np.concatenate((speech.astype(np.int16), np.zeros(48000, dtype=np.int16)))
    for offset in range(0, len(output), 960):
        chunk = output[offset : offset + 960]
        chunk = np.pad(chunk, (0, 960 - len(chunk)))
        await source.capture_frame(
            rtc.AudioFrame(
                data=chunk.tobytes(),
                sample_rate=48000,
                num_channels=1,
                samples_per_channel=len(chunk),
            )
        )
    await source.wait_for_playout()
    return count


async def verify(output: Path, environment: Path) -> None:
    """分开留证语音和打断，日志失败也清理房间；浏览器口型与真人设备需另行验收。"""
    load_dotenv(environment, override=False)
    settings = Settings.from_environment()
    validate_credentials()
    output.mkdir(parents=True, exist_ok=True)
    identity = "synthetic-live2d-verification"
    room_name = "live2d-check-" + uuid.uuid4().hex[:12]
    token = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    room, source, control = await create_verification_resources(
        os.environ["LIVEKIT_URL"], os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    )
    tasks: set[asyncio.Task] = set()
    texts: list[dict] = []
    states: list[dict] = []
    metrics = {"audio_samples": 0, "audio_nonzero_samples": 0, "synthetic_input_samples": 0}
    tracks: Counter[str] = Counter()
    turns: list[dict] = []
    audio_activity: list[dict] = []
    phase = "greeting"
    agent_state = "initializing"
    agent_identity: str | None = None
    started = time.monotonic()

    def elapsed() -> float:
        """所有事件使用同一单调时钟，避免系统校时造成打断时序倒退。"""
        return round(time.monotonic() - started, 4)

    def schedule(coroutine) -> None:
        """事件回调只调度任务；保留任务结果，清理时可以检查媒体读取失败。"""
        tasks.add(asyncio.create_task(coroutine))

    async def read_text(reader, participant: str) -> None:
        """只记录本次合成问题及回复，不采集其他房间或用户的内容。"""
        texts.append({"identity": participant, "text": await reader.read_all(), "time": elapsed()})

    async def read_audio(track) -> None:
        """非零 PCM 才证明下行成功，附带短窗活动数据用于交叉确认打断实际停音。"""
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        try:
            async for event in stream:
                pcm = np.frombuffer(event.frame.data, dtype=np.int16)
                metrics["audio_samples"] += len(pcm)
                metrics["audio_nonzero_samples"] += int(np.count_nonzero(pcm))
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if len(pcm) else 0
                audio_activity.append({"time": elapsed(), "phase": phase, "active": rms > 80})
        finally:
            await stream.aclose()

    def on_track(track, publication, participant) -> None:
        """只统计已派发 Agent 的媒体；当前协议要求音频一条、人物视频零条。"""
        nonlocal agent_identity
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        agent_identity = participant.identity
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tracks["audio"] += 1
            schedule(read_audio(track))
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            tracks["video"] += 1

    def on_attributes(changed, participant) -> None:
        """记录 Agent 的真实状态用于定位时序，用户或其他参与者属性不能影响结论。"""
        nonlocal agent_state, agent_identity
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        agent_identity = participant.identity
        if "lk.agent.state" in changed:
            agent_state = changed["lk.agent.state"]
            states.append({"state": agent_state, "phase": phase, "time": elapsed()})

    def has_reply(since: int, words: tuple[str, ...]) -> bool:
        """回复必须来自同一 Agent，不能把识别出来的用户提问当成模型答案。"""
        return any(
            item["identity"] == agent_identity and any(word in item["text"] for word in words)
            for item in texts[since:]
        )

    room.register_text_stream_handler(
        "lk.transcription", lambda reader, participant: schedule(read_text(reader, participant))
    )
    room.on("track_subscribed", on_track)
    room.on("participant_attributes_changed", on_attributes)
    result: dict = {
        "room_name": room_name,
        "synthetic_audio": True,
        "human_devices_verified": False,
        "browser_live2d_verified": False,
        "lip_sync_verified": False,
    }
    room_created = False
    try:
        await control.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                agents=[api.RoomAgentDispatch(agent_name=settings.agent_name)],
                empty_timeout=35,
                departure_timeout=35,
            )
        )
        room_created = True
        await room.connect(os.environ["LIVEKIT_URL"], token)
        await room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("synthetic-microphone", source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        await wait_until(
            lambda: (
                agent_state == "listening"
                and metrics["audio_nonzero_samples"] > 0
                and has_reply(0, (settings.greeting[:2],))
            )
        )
        prompts = ["你好，请问一加一等于多少？", "请问二加二等于多少？", "停止介绍，请只说你好。"]
        async with httpx.AsyncClient(timeout=90) as client:
            audio = []
            for prompt in prompts:
                response = await client.post(
                    settings.tts_base_url.rstrip("/") + "/audio/speech",
                    headers={"Authorization": "Bearer " + settings.tts_api_key},
                    json={
                        "model": settings.tts_model,
                        "voice": settings.tts_voice,
                        "input": prompt,
                        "response_format": "wav",
                    },
                )
                response.raise_for_status()
                audio.append(response.content)
            for index, markers in enumerate((("二", "2"), ("四", "4"))):
                phase = f"turn-{index + 1}"
                before = len(texts)
                baseline = metrics["audio_nonzero_samples"]
                input_at = elapsed()
                metrics["synthetic_input_samples"] += await publish_wav(source, audio[index])
                await wait_until(lambda before=before, markers=markers: has_reply(before, markers))
                await wait_until(
                    lambda baseline=baseline: (
                        agent_state == "listening" and metrics["audio_nonzero_samples"] > baseline
                    )
                )
                user_texts = [item for item in texts[before:] if item["identity"] == identity]
                if not user_texts:
                    raise AssertionError("合成上行未收到用户识别结果，不能只凭助手回复确认识别")
                turns.append(
                    {
                        "turn": index + 1,
                        "input_at": input_at,
                        "finished_at": elapsed(),
                        "recognized": True,
                        "reply_received": True,
                        "downlink_nonzero_samples": metrics["audio_nonzero_samples"] - baseline,
                    }
                )
            phase = "before-interrupt"
            await room.local_participant.send_text(
                "请用十句话详细介绍春天适合做哪些活动，每句话都解释原因。", topic="lk.chat"
            )
            baseline = metrics["audio_nonzero_samples"]
            await wait_until(
                lambda: (
                    agent_state == "speaking" and metrics["audio_nonzero_samples"] > baseline + 3000
                )
            )
            interrupt_start = len(states)
            text_start = len(texts)
            phase = "interrupt"
            interrupt_at = elapsed()
            metrics["synthetic_input_samples"] += await publish_wav(source, audio[2])
            await wait_until(
                lambda: any(item["state"] == "listening" for item in states[interrupt_start:])
            )
            stopped_at = next(
                item["time"] for item in states[interrupt_start:] if item["state"] == "listening"
            )
            await wait_until(
                lambda: agent_state == "listening" and has_reply(text_start, ("你好",))
            )
            # 状态之后留出网络缓冲排空时间，再检查是否有实际下行静音或帧间空窗。
            after_stop = [
                item
                for item in audio_activity
                if stopped_at + 0.15 <= item["time"] <= stopped_at + 0.4
            ]
            quiet_windows = [item for item in after_stop if not item["active"]]
            result["interruption"] = {
                "input_at": interrupt_at,
                "listening_at": stopped_at,
                "state_stop_delay_ms": round((stopped_at - interrupt_at) * 1000, 1),
                "observed_quiet_windows": len(quiet_windows),
                "observed_no_frames": not after_stop,
                "reply_received": True,
            }
            if stopped_at - interrupt_at > 2.0:
                raise AssertionError("合成输入后 2 秒内未观察到打断状态，可能只是自然播完")
            if after_stop and not quiet_windows:
                raise AssertionError("打断状态后仍只有活动音频，未观察到实际停音证据")
            phase = "finished"
            await asyncio.sleep(0.2)
        assert tracks == {"audio": 1}, tracks
        assert metrics["audio_nonzero_samples"] > 0
        task_errors = [
            str(task.exception())
            for task in tasks
            if task.done() and not task.cancelled() and task.exception() is not None
        ]
        if task_errors:
            raise AssertionError("媒体读取任务失败：" + "; ".join(task_errors))
        result.update(passed=True, completed_voice_turns=2, interrupted=True)
    except BaseException as failure:
        result.update(passed=False, error=type(failure).__name__ + ": " + str(failure))
        raise
    finally:
        result.update(
            metrics=metrics,
            tracks=dict(tracks),
            states=states,
            transcriptions=texts,
            turns=turns,
            audio_activity=audio_activity,
        )
        async with verification_cleanup(
            room=room,
            source=source,
            control=control,
            room_name=room_name,
            created=room_created,
            tasks=tasks,
        ):
            (output / "room-verification.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {key: value for key, value in result.items() if key != "audio_activity"},
                    ensure_ascii=False,
                ),
                flush=True,
            )


def main() -> None:
    """显式命令才连接服务；固定 UTF-8，避免 Windows 默认 GBK 无法输出回复中的 emoji。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env.local"))
    parser.add_argument("--output", type=Path, default=Path(".tools/logs/live2d-room"))
    args = parser.parse_args()
    asyncio.run(verify(args.output, args.env))


if __name__ == "__main__":
    main()
