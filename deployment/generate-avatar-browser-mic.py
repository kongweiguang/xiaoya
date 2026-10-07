"""使用私有 TTS 准备浏览器合成麦克风问题，不接触真人录音或三十条口型素材。"""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx
from dotenv import load_dotenv

from xiaoya.infrastructure.settings import Settings


def load_generator() -> ModuleType:
    """复用现有 PCM 契约与幅度检查，避免第二份生成逻辑漂移到不同的采样率。"""
    path = Path(__file__).with_name("generate-avatar-utterances.py")
    spec = importlib.util.spec_from_file_location("avatar_utterance_generator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("口型合成素材生成模块不可用")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def generate(output: Path, environment: Path) -> None:
    """问题保持固定并分别合成，头尾静音用于浏览器权限准备及本地 VAD 结束本轮发言。"""
    generator = load_generator()
    load_dotenv(environment, override=False)
    settings = Settings.from_environment()
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
        for identifier, text in (
            ("01-one-plus-one", "一加一等于几？"),
            ("02-two-plus-two", "二加二等于几？"),
        ):
            speech, chunks, elapsed = await generator.synthesize(client, settings, text)
            pcm = generator.silence(750) + speech + generator.silence(1200)
            filename = identifier + ".wav"
            digest = generator.write_wav(output / filename, pcm)
            entry = {
                "id": identifier,
                "text": text,
                "file": filename,
                "sha256": digest,
                "sample_rate": generator.SAMPLE_RATE,
                "channels": 1,
                "bits_per_sample": 16,
                "frames": len(pcm) // 2,
                "duration_seconds": len(pcm) / 2 / generator.SAMPLE_RATE,
                "leading_silence_ms": 750,
                "trailing_silence_ms": 1200,
                "stream_chunks": chunks,
                "generation_request_seconds": elapsed,
            }
            entries.append(entry)
            print(json.dumps({"id": identifier, "duration_seconds": entry["duration_seconds"]}))
    manifest = {
        "synthetic_audio": True,
        "contains_user_audio": False,
        "human_devices_verified": False,
        "complete": len(entries) == 2,
        "source": "configured-private-tts",
        "utterances": entries,
    }
    generator.write_manifest(output, manifest)


def main() -> None:
    """帮助检查不会发请求，只有显式运行才使用当前私有服务生成固定的问题素材。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env.local"))
    parser.add_argument(
        "--output", type=Path, default=Path(".tools/live2d-verification/browser-mic")
    )
    arguments = parser.parse_args()
    asyncio.run(generate(arguments.output, arguments.env))


if __name__ == "__main__":
    main()
