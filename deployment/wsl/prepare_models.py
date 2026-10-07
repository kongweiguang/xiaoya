"""在有网络的准备阶段下载权重，语音服务运行时只读取本地文件。"""

import os
from pathlib import Path

from huggingface_hub import snapshot_download

MODELS = Path(os.environ.get("XIAOYA_MODELS_DIR", "/opt/xiaoya/models"))


def main() -> None:
    """在线识别与原生流式合成固定快照，下载仅发生在准备阶段。"""
    MODELS.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id="csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en",
        revision="8e40c43232a1c5c66c82111efc5820d3accca11b",
        local_dir=MODELS / "paraformer-streaming",
        allow_patterns=["*.int8.onnx", "tokens.txt"],
    )
    snapshot_download(
        repo_id="FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
        revision="29e01c4e8d000f4bcd70751be16fa94bf3d85a18",
        local_dir=MODELS / "cosyvoice3-0.5b",
        allow_patterns=[
            "CosyVoice-BlankEN/*",
            "cosyvoice3.yaml",
            "llm.pt",
            "flow.pt",
            "hift.pt",
            "campplus.onnx",
            "speech_tokenizer_v3.onnx",
            "config.json",
            "configuration.json",
        ],
    )


if __name__ == "__main__":
    main()
