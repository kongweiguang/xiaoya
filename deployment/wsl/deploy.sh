#!/usr/bin/env bash
# Windows 和 Linux 的虚拟环境分开保存，防止 WSL 同步覆盖主项目的 Windows .venv。
set -euo pipefail
repo_dir="${1:-/mnt/c/dev/rust/xiaoya}"
runtime_dir=/opt/xiaoya

test -x "$runtime_dir/bin/livekit-server"
test -f "$repo_dir/.env.local"
test -f "$repo_dir/.tools/livekit.yaml"

# 先复用同步预检，避免 vendor 或虚拟环境指向其他项目后才发现链接。
uv run --no-project --python 3.12 \
   "$repo_dir/deployment/sync_runtime.py" "$repo_dir" --configuration --check

mkdir -p "$runtime_dir/agent" "$runtime_dir/speech"
cosyvoice_ref=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
if [[ ! -d "$runtime_dir/vendor/CosyVoice/.git" ]]; then
    git clone --recursive https://github.com/QwenAudio/CosyVoice.git "$runtime_dir/vendor/CosyVoice"
fi
git -C "$runtime_dir/vendor/CosyVoice" checkout "$cosyvoice_ref"
git -C "$runtime_dir/vendor/CosyVoice" submodule update --init --recursive
# 全量部署和日常更新执行同一清单，删除的源码不会残留为第二套实现。
bash "$repo_dir/deployment/wsl/sync-runtime.sh" "$repo_dir" --configuration
uv run --directory "$runtime_dir/speech" --no-sync python \
   "$repo_dir/deployment/wsl/prepare_cosyvoice.py" "$runtime_dir/vendor/CosyVoice"
uv run --directory "$runtime_dir/speech" --no-sync python \
   "$repo_dir/deployment/wsl/prepare_normalizer.py"
uv run --directory "$runtime_dir/agent" --no-sync -m livekit.agents download-files
if [[ ! -f "$runtime_dir/models/paraformer-streaming/encoder.int8.onnx" || \
      ! -f "$runtime_dir/models/cosyvoice3-0.5b/llm.pt" ]]; then
    uv run --directory "$runtime_dir/speech" --no-sync python \
       "$repo_dir/deployment/wsl/prepare_models.py"
fi
cp "$repo_dir/deployment/wsl/xiaoya-livekit.service" \
   "$repo_dir/deployment/wsl/xiaoya-speech.service" \
   "$repo_dir/deployment/wsl/xiaoya-agent.service" /etc/systemd/system/
systemctl daemon-reload
# 部署只管理现行三个单元，不再为已退役部署执行迁移或探测其他进程。
systemctl enable xiaoya-livekit xiaoya-speech xiaoya-agent
systemctl start xiaoya-livekit
systemctl restart xiaoya-speech xiaoya-agent
# Type=notify 的 start 成功意味着 SDK 已注册，不能用 HTTP 200 冒充注册。
systemctl is-active xiaoya-livekit xiaoya-speech xiaoya-agent
