"""历史 PNG 视频方案的素材整理工具，不参与当前 Live2D 或 Agent 部署。"""

import argparse
import hashlib
import shutil
from pathlib import Path

from PIL import Image

ASSET_NAMES = (
    "idle",
    "blink",
    "greeting",
    "listening",
    "thinking",
    "speaking-small",
    "speaking-open",
    "confused",
)


def prepare_assets(project_root: Path) -> None:
    """只规范画布和格式、不重画角色；网页与服务端母版必须保持相同字节。"""
    asset_root = project_root / "src/xiaoya/infrastructure/avatar_assets"
    for name in ASSET_NAMES:
        path = asset_root / f"{name}.png"
        with Image.open(path) as image:
            if image.mode != "RGBA" or image.getextrema()[3][0] != 0:
                raise ValueError(f"{name} 缺少真实透明背景")
            normalized = image.resize((1024, 1024), Image.Resampling.LANCZOS)
        normalized.save(path, optimize=True)
    poster_root = project_root / "web/public/avatar"
    poster_root.mkdir(parents=True, exist_ok=True)
    for name in ("idle", "confused"):
        shutil.copyfile(asset_root / f"{name}.png", poster_root / f"{name}.png")
    poster = poster_root / "idle.png"
    checksum = hashlib.sha256(poster.read_bytes()).hexdigest()
    print(f"已准备 8 张 1024×1024 透明素材，网页母版 SHA-256: {checksum}")


def main() -> None:
    """独立命令便于后续素材更新，默认定位源码工作区而非调用者当前目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    prepare_assets(arguments.project_root)


if __name__ == "__main__":
    main()
