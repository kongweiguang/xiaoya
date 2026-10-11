"""显式私有 TTS 生成三组固定问题，仅做 PCM 边缘裁切与可复核摘要。"""

import argparse
import asyncio
import hashlib
import json
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from benchmark_presentation import private_tts_configuration
from benchmark_turns import PROJECT_ROOT, QUESTION_SPEC, QUESTIONS
from livekit.agents import APIConnectOptions
from livekit.plugins import openai


def trim_edges(pcm: np.ndarray, rate: int) -> tuple[np.ndarray, dict]:
    """依据十毫秒窗 RMS 定位边缘，保留二十毫秒缓冲；不按文字猜音素切点。"""
    if not len(pcm) or np.max(np.abs(pcm.astype(np.float64))) < 500:
        raise ValueError("合成音频没有有效信号")
    window = max(1, round(rate * 0.01))
    padded = np.pad(pcm.astype(np.float64), (0, (-len(pcm)) % window))
    rms = np.sqrt(np.mean(padded.reshape(-1, window) ** 2, axis=1))
    threshold = max(80.0, float(np.max(rms)) * 0.025)
    active = np.flatnonzero(rms >= threshold)
    padding = round(rate * 0.02)
    start = max(0, int(active[0]) * window - padding)
    stop = min(len(pcm), (int(active[-1]) + 1) * window + padding)
    result = pcm[start:stop]
    clipped = float(np.mean(np.abs(result.astype(np.float64)) >= 32767))
    if clipped > 0.005 or len(result) / rate < 0.25:
        raise ValueError("音频削波或时长不足，禁止标记为可用素材")
    return result, {
        "kind": "tts-input-and-window-rms-bounds",
        "human_listened": False,
        "window_ms": 10,
        "padding_ms": 20,
        "threshold_rms": threshold,
        "source_frames": len(pcm),
        "crop_from_frame": start,
        "crop_to_frame": stop,
        "peak": int(np.max(np.abs(result.astype(np.float64)))),
        "clipped_ratio": clipped,
    }


def save_wave(path: Path, pcm: np.ndarray, rate: int) -> str:
    """明确保存单声道 PCM 与文件摘要，原始和裁切素材都能逐样本复核。"""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm.astype("<i2").tobytes())
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def prepare(config: dict, destination: Path) -> dict:
    """六请求串行且完全消费音频，所有请求共用一个显式私有客户端并最终关闭。"""
    destination.mkdir(parents=True, exist_ok=True)
    client = openai.TTS(**config)
    requests, streams_closed, frames_consumed = 0, 0, 0
    questions = []
    try:
        for identifier, prefix, suffix in QUESTION_SPEC:
            item = {"id": identifier, "prefix_text": prefix, "suffix_text": suffix}
            for part, text in (("prefix", prefix), ("suffix", suffix)):
                frames, started = [], time.monotonic()
                rate = None
                requests += 1
                async with client.synthesize(
                    text, conn_options=APIConnectOptions(timeout=30, max_retry=0)
                ) as stream:
                    async for event in stream:
                        audio = event.frame
                        if audio.num_channels != 1 or (rate and rate != audio.sample_rate):
                            raise ValueError("私有 TTS 返回非单声道或变化采样率")
                        rate = audio.sample_rate
                        frames.append(np.frombuffer(audio.data, dtype="<i2").copy())
                        frames_consumed += 1
                streams_closed += 1
                if not frames:
                    raise ValueError("私有 TTS 没有音频帧")
                original = np.concatenate(frames)
                pcm, review = trim_edges(original, rate)
                source_file = f"{identifier}-{part}-source.wav"
                name = f"{identifier}-{part}.wav"
                review.update(
                    {
                        "source_file": source_file,
                        "source_sha256": save_wave(destination / source_file, original, rate),
                        "sample_rate": rate,
                        "tts_input": text,
                        "generation_seconds": time.monotonic() - started,
                        "duration_seconds": len(pcm) / rate,
                    }
                )
                item.update(
                    {
                        f"{part}_file": name,
                        f"{part}_sha256": save_wave(destination / name, pcm, rate),
                        f"{part}_review": review,
                    }
                )
                print(
                    json.dumps({"id": identifier, "part": part, **review}, ensure_ascii=False),
                    flush=True,
                )
            item["trimmed_and_reviewed"] = True
            questions.append(item)
    finally:
        await client.aclose()
    document = {
        "version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "synthetic_audio": True,
        "contains_user_audio": False,
        "human_listened": False,
        "source": {"kind": "private-tts", "model": config["model"], "voice": config["voice"]},
        "review_scope": "合成输入语义标注 + RMS 边界裁切与削波检查；未由真人收听或识别模型确认",
        "resources": {
            "requests": requests,
            "streams_closed": streams_closed,
            "frames_consumed": frames_consumed,
            "clients_created": 1,
            "clients_closed": 1,
        },
        "questions": questions,
    }
    (destination / "manifest.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return document


def main(argv: list[str] | None = None) -> int:
    """必须显式选择私有合成，输出限定于被忽略目录，不读取 LLM、Jev 或房间配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-tts", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--output", type=Path, default=QUESTIONS.parent)
    args = parser.parse_args(argv)
    destination = args.output.resolve()
    if not destination.is_relative_to((PROJECT_ROOT / ".tools").resolve()):
        parser.error("素材仅保存到项目被忽略的 .tools 目录")
    if (destination / "manifest.json").exists():
        parser.error("已存在正式素材清单，使用新的 --output 目录避免覆盖验收证据")
    try:
        result = asyncio.run(prepare(private_tts_configuration(args.env_file), destination))
        print(
            json.dumps(
                {"manifest": str(destination / "manifest.json"), "resources": result["resources"]},
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as error:
        print(f"素材生成失败（{type(error).__name__}）；没有输出成功清单。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
