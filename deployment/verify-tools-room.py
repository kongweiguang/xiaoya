"""用合成音频和固定文字验收内置工具，演示 MCP 仅在显式选择后执行。"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents.voice.remote_session import RemoteSession
from verification_room import (
    create_verification_resources,
    publish_wav,
    verification_cleanup,
    wait_until,
)

from xiaoya.infrastructure.settings import Settings, validate_credentials


async def verify(environment: Path, output: Path, *, demo: bool = False) -> None:
    """只读取专属合成验收房间，使用 SDK 远程事件证明工具确实执行，避免把模型猜测当成功。"""
    load_dotenv(environment, override=False)
    settings = Settings.from_environment()
    validate_credentials()
    room_name = "mcp-tools-check-" + uuid.uuid4().hex[:10]
    identity = "synthetic-tools-verification"
    token = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    room, source, control = await create_verification_resources(
        os.environ["LIVEKIT_URL"], os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    )
    remote = None
    tasks = set()
    texts = []
    calls = []
    metrics = {
        "upstream_samples_48k": 0,
        "downstream_samples_16k": 0,
        "downstream_nonzero_samples": 0,
    }
    state = "initializing"
    agent_identity = None
    result = {
        "passed": False,
        "synthetic_audio": True,
        "human_devices_verified": False,
        "room": room_name,
        "turns": [],
    }

    def schedule(coroutine) -> None:
        """同步回调只调度任务，异步读取不能阻塞 SDK 的媒体事件。"""
        tasks.add(asyncio.create_task(coroutine))

    async def read_text(reader, participant: str) -> None:
        """本次固定问题与回复可保存为证据，其他房间的用户内容不进入读取范围。"""
        texts.append({"identity": participant, "text": await reader.read_all()})

    async def read_audio(track) -> None:
        """累计非零 PCM，实际下行声音比订阅事件更能证明合成与传输成功。"""
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        try:
            async for event in stream:
                pcm = np.frombuffer(event.frame.data, dtype=np.int16)
                metrics["downstream_samples_16k"] += len(pcm)
                metrics["downstream_nonzero_samples"] += int(np.count_nonzero(pcm))
        finally:
            await stream.aclose()

    def on_track(track, publication, participant) -> None:
        """只验收已派发 Agent 的音频，网页人物采用自身渲染时不要求视频轨道。"""
        nonlocal agent_identity
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_identity = participant.identity
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                schedule(read_audio(track))

    def on_attributes(changed, participant) -> None:
        """用真实 Agent 状态安排下一轮，避免测试问题与上一次语音重叠。"""
        nonlocal state, agent_identity
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_identity = participant.identity
            state = changed.get("lk.agent.state", state)

    def on_tools(event) -> None:
        """SDK 返回的调用与结果才算执行证据，保存的结果仅来自固定合成文案。"""
        executed = event.function_tools_executed
        for call, response in zip(
            executed.function_calls, executed.function_call_outputs, strict=True
        ):
            calls.append(
                {"name": call.name, "is_error": response.is_error, "output": response.output}
            )

    room.register_text_stream_handler(
        "lk.transcription", lambda reader, participant: schedule(read_text(reader, participant))
    )
    room.on("track_subscribed", on_track)
    room.on("participant_attributes_changed", on_attributes)
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
            rtc.LocalAudioTrack.create_audio_track("synthetic-tools-microphone", source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        await wait_until(
            lambda: (
                agent_identity
                and state == "listening"
                and texts
                and metrics["downstream_nonzero_samples"] > 0
            )
        )
        remote = RemoteSession.from_room(room, agent_identity)
        remote.on("function_tools_executed", on_tools)
        await remote.start()
        await remote.wait_for_ready(timeout=15)
        info = await remote.get_agent_info()
        result["available_tools"] = list(info.tools)
        cases = (
            ("voice", "calculate", "请用计算工具计算二十三乘以七。", "161"),
            (
                "voice",
                "save_note",
                "请保存一条便签，标题是出门准备，内容是带耳机和充电器。",
                "耳机",
            ),
            ("voice", "list_notes", "请调用便签工具，查一下我本次通话记了什么。", "充电器"),
            ("text", "current_time", "请查询实际的北京时间和日期。", "Asia/Shanghai"),
            ("text", "delete_note", "请删除标题为出门准备的便签。", "True"),
        )
        if demo:
            cases += (
                ("voice", "demo__search_knowledge", "请用知识库工具查询便签能否永久保存。", "通话"),
                (
                    "text",
                    "demo__get_demo_ticket",
                    "请调用工单工具查询演示工单 DEMO-001。",
                    "已受理",
                ),
            )
        assert {case[1] for case in cases} <= set(info.tools), "部署后的 Agent 缺少工具"
        async with httpx.AsyncClient(timeout=90) as client:
            for mode, expected, prompt, marker in cases:
                before_text, before_calls = len(texts), len(calls)
                baseline = metrics["downstream_nonzero_samples"]
                if mode == "voice":
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
                    metrics["upstream_samples_48k"] += await publish_wav(source, response.content)
                else:
                    await room.local_participant.send_text(prompt, topic="lk.chat")

                def turn_finished(
                    expected: str = expected,
                    before_calls: int = before_calls,
                    before_text: int = before_text,
                    baseline: int = baseline,
                ) -> bool:
                    """绑定本轮起点，避免等待期间把其他轮次的工具或音频误判为本轮完成。"""
                    return (
                        any(call["name"] == expected for call in calls[before_calls:])
                        and any(item["identity"] == agent_identity for item in texts[before_text:])
                        and state == "listening"
                        and metrics["downstream_nonzero_samples"] > baseline
                    )

                await wait_until(turn_finished)
                selected = [call for call in calls[before_calls:] if call["name"] == expected]
                assert selected and all(not call["is_error"] for call in selected), "工具返回失败"
                assert any(marker in call["output"] for call in selected), "工具结果与预期不符"
                if mode == "voice":
                    assert any(item["identity"] == identity for item in texts[before_text:]), (
                        "缺少语音识别结果"
                    )
                result["turns"].append(
                    {
                        "input": mode,
                        "tool": expected,
                        "passed": True,
                        "transcriptions": texts[before_text:],
                        "downstream_nonzero_samples": metrics["downstream_nonzero_samples"]
                        - baseline,
                    }
                )
                print(json.dumps({"tool": expected, "input": mode, "passed": True}), flush=True)
        result["passed"] = True
    except BaseException as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        result.update(metrics=metrics, tool_calls=calls)
        async with verification_cleanup(
            room=room,
            source=source,
            control=control,
            room_name=room_name,
            created=room_created,
            tasks=tasks,
            remote=remote,
        ):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    """显式连接真实服务并固定 UTF-8，避免 Windows 控制台编码导致验收输出失败。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env.local"))
    parser.add_argument("--demo", action="store_true", help="额外验收显式配置的演示 MCP")
    parser.add_argument(
        "--output", type=Path, default=Path(".tools/logs/mcp-room-verification.json")
    )
    args = parser.parse_args()
    asyncio.run(verify(args.env, args.output, demo=args.demo))


if __name__ == "__main__":
    main()
