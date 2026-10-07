"""使用已配置的私有 TTS 生成固定中文验收语音，不采集用户音频或保存凭据。"""

import argparse
import asyncio
import hashlib
import json
import math
import struct
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

from xiaoya.infrastructure.settings import Settings

SAMPLE_RATE = 24000
LEADING_SILENCE_MS = 250
TRAILING_SILENCE_MS = 500


@dataclass(frozen=True, slots=True)
class Utterance:
    """固定合成文案与可控停顿组成测量样本，避免把真人音频混入验收材料。"""

    identifier: str
    category: str
    segments: tuple[str, ...]
    pause_ms: int = 0


def cases() -> tuple[Utterance, ...]:
    """同时覆盖短促发声、闭唇、圆唇、明确静音、连续中文和跨多句的长回复。"""
    return (
        Utterance("01-short-a", "short-syllable", ("阿姨。",)),
        Utterance("02-short-yi", "short-syllable", ("一。",)),
        Utterance("03-short-wu", "short-syllable", ("乌云。",)),
        Utterance("04-short-e", "short-syllable", ("鹅。",)),
        Utterance("05-short-o", "short-syllable", ("哦。",)),
        Utterance("06-short-ba", "short-syllable", ("八。",)),
        Utterance("07-short-pa", "short-syllable", ("怕。",)),
        Utterance("08-short-ma", "short-syllable", ("妈。",)),
        Utterance("09-short-fa", "short-syllable", ("发。",)),
        Utterance("10-short-ta", "short-syllable", ("他。",)),
        Utterance("11-short-si", "short-syllable", ("四。",)),
        Utterance("12-short-zhi", "short-syllable", ("知。",)),
        Utterance("13-pause-greeting", "explicit-pause", ("你好。", "我在听。"), 400),
        Utterance("14-pause-thinking", "explicit-pause", ("请稍等。", "我想一想。"), 600),
        Utterance("15-pause-numbers", "explicit-pause", ("一、二、三。", "四、五、六。"), 800),
        Utterance("16-pause-resume", "explicit-pause", ("先停一下。", "我们继续。"), 500),
        Utterance("17-pause-thanks", "explicit-pause", ("好的。", "谢谢你。"), 700),
        Utterance(
            "18-pause-three-segments",
            "explicit-pause",
            ("今天阳光很好。", "下午一起散步。", "记得带水。"),
            500,
        ),
        Utterance(
            "19-continuous-greeting", "continuous-chinese", ("你好，我是小芽，很高兴见到你。",)
        ),
        Utterance(
            "20-continuous-question", "continuous-chinese", ("请说一说你今天最想完成的一件事。",)
        ),
        Utterance(
            "21-continuous-weather", "continuous-chinese", ("天气变凉了，出门前记得带一件外套。",)
        ),
        Utterance(
            "22-continuous-steps", "continuous-chinese", ("把复杂的事情分成几小步，就更容易开始。",)
        ),
        Utterance(
            "23-continuous-calm", "continuous-chinese", ("不用着急，我们可以一句一句慢慢聊。",)
        ),
        Utterance(
            "24-continuous-breath", "continuous-chinese", ("先深呼吸，再告诉我刚才发生了什么。",)
        ),
        Utterance(
            "25-continuous-calendar", "continuous-chinese", ("今天是星期二，距离周末还有几天。",)
        ),
        Utterance(
            "26-continuous-answer",
            "continuous-chinese",
            ("我听见了你的问题，现在给你一个简短的回答。",),
        ),
        Utterance(
            "27-long-planning",
            "long-reply",
            (
                "如果今天的任务很多，可以先写下最重要的三件事。"
                "把第一件事拆成一个能够马上开始的小步骤，做完之后再安排下一步。"
                "遇到不确定的地方，先把问题说清楚，找一个可以验证的方法。"
                "完成工作后留一点时间休息，喝水，活动一下肩膀。"
                "你不需要一次做好所有事情，保持自己的节奏就很好。",
            ),
        ),
        Utterance(
            "28-long-explanation",
            "long-reply",
            (
                "我们可以按三个步骤理解这个问题。第一步，确认已经知道的信息。"
                "第二步，把暂时不清楚的部分列出来，再逐个寻找证据。"
                "第三步，比较不同做法的结果，选择适合当前情况的一种。"
                "如果中途出现新的信息，可以调整判断，也可以从最小的实验重新开始。",
            ),
        ),
        Utterance(
            "29-long-outdoors",
            "long-reply",
            (
                "春天适合在天气晴朗的时候到户外走一走。"
                "选择一条熟悉的小路，看看树叶的颜色，听一听周围的声音。"
                "出门前确认天气，带好饮水和合适的外套。"
                "如果路面湿滑，就放慢脚步，选择安全的地方休息。"
                "散步不需要很远，和朋友聊聊天，或者安静地感受一会儿阳光，都可以。",
            ),
        ),
        Utterance(
            "30-long-conversation",
            "long-reply",
            (
                "聊天的时候，你可以先说最想表达的一句话，我会认真听。"
                "如果你想停下来想一想，也没有关系，我们可以等一会儿再继续。"
                "当我的回答太长，你可以直接打断，告诉我需要更短的说明。"
                "如果我理解错了，请指出是哪一部分，我们一起把它说清楚。"
                "现在，你想从今天的工作，生活，还是一个新想法开始呢？",
            ),
        ),
    )


def silence(milliseconds: int) -> bytes:
    """静音按采样数构造，明确停顿不依赖 TTS 对标点的随机时长解释。"""
    return bytes(round(SAMPLE_RATE * milliseconds / 1000) * 2)


def maximum_window_rms(pcm: bytes) -> float:
    """独立十毫秒能量检查拒绝近乎无声的合成结果，不人为放大输入来美化口型测量。"""
    size = round(SAMPLE_RATE * 0.01) * 2
    maximum = 0.0
    for offset in range(0, len(pcm), size):
        window = pcm[offset : offset + size]
        values = [value[0] / 32768 for value in struct.iter_unpack("<h", window)]
        if values:
            maximum = max(maximum, math.sqrt(sum(value * value for value in values) / len(values)))
    return maximum


def reusable_entry(
    item: Utterance, recorded: dict[str, object] | None, output: Path
) -> dict[str, object] | None:
    """只复用内容、指纹、格式和有声条件均一致的结果，失败样本重新合成仍保持完整三十条。"""
    if not recorded or recorded.get("text") != "".join(item.segments):
        return None
    path = output / (item.identifier + ".wav")
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != recorded.get("sha256"):
            return None
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getframerate() != SAMPLE_RATE
                or wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getnframes() != recorded.get("frames")
            ):
                return None
            pcm = wav.readframes(wav.getnframes())
        if maximum_window_rms(pcm) <= 0.012:
            return None
    except (OSError, wave.Error, EOFError):
        return None
    return recorded


async def synthesize(
    client: httpx.AsyncClient, settings: Settings, text: str
) -> tuple[bytes, int, float]:
    """读取现行流式 PCM 契约，不把认证、URL 或服务响应正文写进错误及测量记录。"""
    started = time.monotonic()
    chunks = 0
    pcm = bytearray()
    try:
        async with client.stream(
            "POST",
            settings.tts_base_url.rstrip("/") + "/audio/speech",
            headers={"Authorization": "Bearer " + (settings.tts_api_key or "not-required")},
            json={
                "model": settings.tts_model,
                "voice": settings.tts_voice,
                "input": text,
                "response_format": "pcm",
                "speed": 1.0,
            },
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"私有 TTS 返回 HTTP {response.status_code}")
            if not response.headers.get("content-type", "").startswith("audio/pcm"):
                raise RuntimeError("私有 TTS 未返回约定的流式 PCM")
            async for chunk in response.aiter_bytes():
                pcm.extend(chunk)
                chunks += 1
    except httpx.HTTPError as error:
        raise RuntimeError(f"私有 TTS 网络请求失败：{type(error).__name__}") from None
    if not pcm or len(pcm) % 2:
        raise RuntimeError("私有 TTS 返回空音频或不完整的 PCM16 样本")
    if maximum_window_rms(bytes(pcm)) <= 0.012:
        raise RuntimeError("私有 TTS 返回的声音低于实际口型静音门限，请使用带语境的合成文案")
    return bytes(pcm), chunks, time.monotonic() - started


def write_wav(path: Path, pcm: bytes) -> str:
    """先完整写临时容器再原子替换，取消或磁盘错误不能留下可误读的半个 WAV。"""
    temporary = path.with_suffix(".wav.part")
    with wave.open(str(temporary), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(output: Path, manifest: dict[str, object]) -> None:
    """每条成功后记录进度；complete 只有全部三十条存在时为真，不夸大部分生成结果。"""
    temporary = output / "manifest.json.part"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output / "manifest.json")


async def generate(output: Path, environment: Path, reuse_valid: bool = False) -> None:
    """顺序使用私有模型锁，固定合成输入与清晰元数据可供浏览器重复测量。"""
    load_dotenv(environment, override=False)
    settings = Settings.from_environment()
    output.mkdir(parents=True, exist_ok=True)
    reusable: dict[str, dict[str, object]] = {}
    if reuse_valid and (output / "manifest.json").exists():
        previous = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        source = previous.get("source", {})
        if (
            source.get("kind") == "private-tts"
            and source.get("model") == settings.tts_model
            and source.get("voice") == settings.tts_voice
        ):
            reusable = {entry["id"]: entry for entry in previous.get("utterances", [])}
    entries: list[dict[str, object]] = []
    manifest: dict[str, object] = {
        "version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "complete": False,
        "synthetic_audio": True,
        "contains_user_audio": False,
        "browser_verified": False,
        "human_devices_verified": False,
        "source": {
            "kind": "private-tts",
            "model": settings.tts_model,
            "voice": settings.tts_voice,
            "response_format": "pcm",
        },
        "sample_format": "PCM_S16LE",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "leading_silence_ms": LEADING_SILENCE_MS,
        "trailing_silence_ms": TRAILING_SILENCE_MS,
        "utterances": entries,
    }
    write_manifest(output, manifest)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
        for item in cases():
            retained = reusable_entry(item, reusable.get(item.identifier), output)
            if retained:
                entries.append(retained)
                write_manifest(output, manifest)
                print(json.dumps({"id": item.identifier, "reused": True}), flush=True)
                continue
            pcm = bytearray(silence(LEADING_SILENCE_MS))
            segments: list[dict[str, object]] = []
            pauses: list[dict[str, object]] = []
            chunks = 0
            request_seconds = 0.0
            for index, text in enumerate(item.segments):
                if index:
                    pauses.append(
                        {"start_seconds": len(pcm) / 2 / SAMPLE_RATE, "duration_ms": item.pause_ms}
                    )
                    pcm.extend(silence(item.pause_ms))
                start = len(pcm) / 2 / SAMPLE_RATE
                speech, received_chunks, elapsed = await synthesize(client, settings, text)
                pcm.extend(speech)
                chunks += received_chunks
                request_seconds += elapsed
                segments.append(
                    {"text": text, "from_seconds": start, "to_seconds": len(pcm) / 2 / SAMPLE_RATE}
                )
            pcm.extend(silence(TRAILING_SILENCE_MS))
            filename = item.identifier + ".wav"
            digest = write_wav(output / filename, bytes(pcm))
            entry = {
                "id": item.identifier,
                "type": item.category,
                "text": "".join(item.segments),
                "file": filename,
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "bits_per_sample": 16,
                "frames": len(pcm) // 2,
                "duration_seconds": round(len(pcm) / 2 / SAMPLE_RATE, 6),
                "sha256": digest,
                "segments": segments,
                "inserted_silences": pauses,
                "stream_chunks": chunks,
                "generation_request_seconds": round(request_seconds, 4),
            }
            entries.append(entry)
            write_manifest(output, manifest)
            print(
                json.dumps(
                    {key: entry[key] for key in ("id", "type", "sample_rate", "duration_seconds")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    manifest["complete"] = len(entries) == 30
    write_manifest(output, manifest)
    print(json.dumps({"complete": manifest["complete"], "utterances": len(entries)}), flush=True)


def main() -> None:
    """真实请求只由显式生成命令触发，帮助和普通离线测试不会连接模型服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env.local"))
    parser.add_argument(
        "--output", type=Path, default=Path(".tools/live2d-verification/utterances")
    )
    parser.add_argument(
        "--reuse-valid",
        action="store_true",
        help="验证并复用已有声音，只重新生成无效或文案变化样本",
    )
    args = parser.parse_args()
    asyncio.run(generate(args.output, args.env, args.reuse_valid))


if __name__ == "__main__":
    main()
