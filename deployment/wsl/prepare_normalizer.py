"""在安装阶段准备内容哈希固定的文本规范化规则，通话期间不联网。"""

import hashlib
import os
import subprocess
from pathlib import Path

FILES = {
    "zh/tn/tagger.fst": "cf341314c51f7ce59049f3b2c42f0ce8fd71e6d08d4d6969613aad384a5e2ae8",
    "zh/tn/verbalizer.fst": "5a13cd679dd54637d12d2bd1bd33ee2165d91c867e14468c93195af02256e5da",
    "en/tn/tagger.fst": "245e2dc9174cdd007a8e9e50f3339773d1adbbc7535b71cf67478dc1683cc3ec",
    "en/tn/verbalizer.fst": "03155c88f317b2795969e264c19f87faf98b9853d5ac631e419bacdc3b3ee15a",
}


def main() -> None:
    """官方文件按 SHA-256 锁定，镜像内容变化时拒绝替换已验证的规则。"""
    root = Path(os.environ.get("XIAOYA_MODELS_DIR", "/opt/xiaoya/models")) / "wetext"
    for name, expected in FILES.items():
        target = root / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(".download")
        subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--retry",
                "3",
                "--max-time",
                "180",
                "--noproxy",
                "*",
                "--user-agent",
                "curl/8.16.0",
                "--output",
                str(pending),
                f"https://modelscope.cn/models/pengzhendong/wetext/resolve/master/{name}",
            ],
            check=True,
        )
        if hashlib.sha256(pending.read_bytes()).hexdigest() != expected:
            pending.unlink()
            raise RuntimeError(f"文本规范化文件哈希不一致: {name}")
        pending.replace(target)
    print("Local text normalization files verified")


if __name__ == "__main__":
    main()
