# 私有运行与部署

唯一常驻方式是 systemd 管理 `xiaoya-livekit`、`xiaoya-speech`、`xiaoya-agent`。Windows 启动器只启动、检查并跟随日志，不另起前台 Agent，不自动调用付费 LLM。

## 目录与初次部署

Windows 源码为 `C:\dev\rust\xiaoya`；WSL 运行目录为 `/opt/xiaoya`。两侧 `.venv` 严格分离。先准备 NVIDIA CUDA、私有 LiveKit 二进制 `/opt/xiaoya/bin/livekit-server`、项目 `.tools/livekit.yaml` 和被忽略的 `.env.local`；模型准备脚本只下载此项目模型。

```powershell
wsl -d Ubuntu-22.04 -- bash /mnt/c/dev/rust/xiaoya/deployment/wsl/deploy.sh /mnt/c/dev/rust/xiaoya
```

部署固定已有 SDK 和 CosyVoice 版本，不升级其他服务。`deploy.sh` 同步代码与显式配置，准备项目模型，安装三个项目单元并重启 speech／Agent。退役的本项目 Ollama 服务不会重新启用。

`deployment/sync_runtime.py` 是全量部署与日常更新共用的受管清单：只镜像 Agent、speech、独立示例源码及列出的项目文件。执行前校验绝对目标、路径边界和链接，删除仅发生在受管源码目录。运行环境、模型、vendor、私有配置以及其他服务不在日常清理范围；复制本机配置必须额外使用 `--configuration`。

## 日常更新

```powershell
wsl -d Ubuntu-22.04 -- bash /mnt/c/dev/rust/xiaoya/deployment/wsl/sync-runtime.sh /mnt/c/dev/rust/xiaoya
wsl -d Ubuntu-22.04 -- systemctl restart xiaoya-speech xiaoya-agent
pwsh -File deployment/start-wsl.ps1
```

源码删除会同步到运行目录，不能手工复制单个文件留下旧实现。需要更新凭据时，先核对两侧私有配置，再显式执行 `sync-runtime.sh <repo> --configuration`。示例 MCP 默认不接入；开启前配置自己的 `mcp.local.json`。

Agent 的 `ExecStartPre` 等待 speech 最长 300 秒；整体 `TimeoutStartSec=330`。`Type=notify` 仅在 SDK 公开 `worker_registered` 事件后发送 READY；HTTP 200 或旧进程存活不能证明 Agent 已注册。重新启动 speech 后同时重启 Agent。

speech 与 Agent 健康接口绑定 `127.0.0.1`。LiveKit 信令与媒体必须保持 Windows 可达，不能一起改成回环。网页启动器读取 WSL eth0 地址，只传入本次子进程环境，不反复改写 `web/.env.local`。启动失败只清理本次创建的网页进程。

## 安全与运维

- 根配置中 STT／TTS 密钥不可留空；无鉴权填 `not-required`。DeepSeek 必须真实独立密钥，不从其他模型配置继承。
- 本地语音要求 CUDA；`SPEECH_TTS_ACCELERATION` 明确为 `vllm` 或 `none`。保留 CosyVoice 实际需要的 Whisper、Matcha 等依赖。
- 默认网页监听本机。令牌只接受浏览器 `POST {}`，服务端决定房间、身份、Agent 与最小权限，15 分钟过期，离房清理窗口为 35 秒。
- 远程网页须经已认证网关／隧道。服务端同时配置 `TOKEN_GATEWAY_ORIGIN`、`TOKEN_GATEWAY_KEY`；网关鉴权后覆盖注入 `x-xiaoya-gateway-key`。必须拒绝客户端伪造的同名头，密钥不能下发浏览器。仅做端口转发不是鉴权。
- LiveKit 成功响应遵循 [官方令牌接口契约](https://docs.livekit.io/frontends/build/authentication/endpoint/)；失败信息统一且不包含密钥，响应禁止缓存。

检查本项目单元与日志：

```powershell
wsl -d Ubuntu-22.04 -- systemctl status xiaoya-livekit xiaoya-speech xiaoya-agent
wsl -d Ubuntu-22.04 -- journalctl -u xiaoya-agent -u xiaoya-speech -n 80 --no-pager
```

不要停止其他服务，不要强制删除调度或使用 SDK 私有字段制造验收通过。

## 显式诊断与真实房间验证

诊断会实际访问配置的接口；LLM 调用计费，绝不在每次启动自动运行：

```powershell
uv run python deployment/diagnose_llm.py
uv run python deployment/verify-tools.py
uv run python deployment/verify-tools-room.py
uv run python deployment/verify-avatar-room.py
```

工具默认验证五个内置工具，`--demo` 才额外连接独立示例 MCP。房间脚本创建自己的房间与连接，报告写入失败也会逐项清理。注入合成测试音频不算真人设备验收；完整房间、模型工程、真人设备结果分别记录于 [验收记录](../docs/delivery-acceptance.md)。

需要口型或浏览器虚拟麦克风的固定素材时，显式调用私有 TTS：

```powershell
uv run python deployment/generate-avatar-utterances.py --reuse-valid
uv run python deployment/generate-avatar-browser-mic.py
```

前者准备 30 条口型素材，默认写入 `.tools/live2d-verification/utterances`，`--reuse-valid` 仅复用校验通过且文案一致的已有样本。后者准备 2 条固定提问，默认写入 `.tools/live2d-verification/browser-mic`；它们都是合成音频，不采集真人录音，也不算真人设备验收。输出为本机忽略文件，不随源码推送。两个脚本均支持 `--env`、`--output`；`--help` 不访问模型服务。
