#!/usr/bin/env bash
# Windows 和 Linux 的虚拟环境分开保存，防止 WSL 同步覆盖主项目的 Windows .venv。
set -euo pipefail
repo_dir="${1:-/mnt/c/dev/rust/xiaoya}"
runtime_dir=/opt/xiaoya

test -x "$runtime_dir/bin/livekit-server"
test -f "$repo_dir/.env.local"
test -f "$repo_dir/.tools/livekit.yaml"

mkdir -p "$runtime_dir/agent/src" "$runtime_dir/speech/src"
cosyvoice_ref=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
if [[ ! -d "$runtime_dir/vendor/CosyVoice/.git" ]]; then
    git clone --recursive https://github.com/QwenAudio/CosyVoice.git "$runtime_dir/vendor/CosyVoice"
fi
git -C "$runtime_dir/vendor/CosyVoice" checkout "$cosyvoice_ref"
git -C "$runtime_dir/vendor/CosyVoice" submodule update --init --recursive
cp "$repo_dir/pyproject.toml" "$repo_dir/uv.lock" "$repo_dir/README.md" "$repo_dir/mcp.example.json" \
   "$repo_dir/.python-version" "$repo_dir/.env.local" "$runtime_dir/agent/"
if [[ -f "$repo_dir/mcp.local.json" ]]; then
    cp "$repo_dir/mcp.local.json" "$runtime_dir/agent/"
fi
cp -a "$repo_dir/src/." "$runtime_dir/agent/src/"
cp "$repo_dir/services/speech/pyproject.toml" "$repo_dir/services/speech/uv.lock" \
   "$repo_dir/services/speech/.python-version" "$runtime_dir/speech/"
cp -a "$repo_dir/services/speech/src/." "$runtime_dir/speech/src/"
cp "$repo_dir/.tools/livekit.yaml" "$runtime_dir/livekit.yaml"
cp "$repo_dir/deployment/wsl/start-livekit.sh" "$runtime_dir/start-livekit.sh"
cp "$repo_dir/deployment/wsl/prewarm_llm.py" "$runtime_dir/prewarm-llm.py"
chmod 600 "$runtime_dir/livekit.yaml" "$runtime_dir/agent/.env.local"

# 首次或重复完整部署也复用精确同步，避免 cp -a 留下旧视频模块、PNG 和旧依赖。
# 入口与验收脚本需要安装项目本身；同步工具保留 Linux 环境的 editable 路径。
bash "$repo_dir/deployment/wsl/sync-agent.sh" "$repo_dir"
uv sync --directory "$runtime_dir/speech" --locked --no-dev --no-install-project
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
# 对话模型已切换 DeepSeek，重复部署不得重新启动项目旧的 GPU LLM。
if systemctl cat xiaoya-ollama.service >/dev/null 2>&1; then
    systemctl disable --now xiaoya-ollama
fi
systemctl enable --now xiaoya-livekit xiaoya-speech
systemctl restart xiaoya-speech
systemctl is-active xiaoya-livekit xiaoya-speech
