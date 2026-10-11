# 私有运行与部署

唯一常驻方式是 systemd 管理 `xiaoya-livekit`、`xiaoya-speech`、`xiaoya-agent`。Windows 启动器只启动、检查并跟随日志，不另起前台 Agent，不自动调用付费 LLM。

使用 `start-wsl.ps1` 时保持日志窗口打开：它的前台日志跟随维持 WSL 实例运行。关闭最后一个 WSL 前台命令后，实例可能自动退出，三个服务也会随之停止；systemd 服务本身不维持实例存活。[Microsoft 官方说明](https://learn.microsoft.com/en-us/windows/wsl/systemd) 与当前测试的实例退出行为一致。验收脚本仅用自己持有的只读前台命令保活，不修改全局 WSL 设置或另起 Agent。

## 目录与初次部署

Windows 源码为 `C:\kong\personal-projects\小芽\代码\xiaoya`；WSL 运行目录为 `/opt/xiaoya`。两侧 `.venv` 严格分离。先准备 NVIDIA CUDA、私有 LiveKit 二进制 `/opt/xiaoya/bin/livekit-server`、项目 `.tools/livekit.yaml` 和被忽略的 `.env.local`；模型准备脚本只下载此项目模型。

```powershell
wsl -d Ubuntu-22.04 -- bash -lc 'bash /mnt/c/kong/personal-projects/小芽/代码/xiaoya/deployment/wsl/deploy.sh /mnt/c/kong/personal-projects/小芽/代码/xiaoya'
```

部署固定已有 SDK 和 CosyVoice 版本，不升级其他服务。`deploy.sh` 同步代码与显式配置，准备项目模型，安装三个项目单元并重启 speech／Agent。退役的本项目 Ollama 服务不会重新启用。

`deployment/sync_runtime.py` 是全量部署与日常更新共用的受管清单：只镜像 Agent、speech、独立示例源码及列出的项目文件。执行前校验绝对目标、路径边界和链接，删除仅发生在受管源码目录。运行环境、模型、vendor、私有配置以及其他服务不在日常清理范围；复制本机配置必须额外使用 `--configuration`。

## 日常更新

```powershell
wsl -d Ubuntu-22.04 -- bash -lc 'bash /mnt/c/kong/personal-projects/小芽/代码/xiaoya/deployment/wsl/sync-runtime.sh /mnt/c/kong/personal-projects/小芽/代码/xiaoya'
wsl -d Ubuntu-22.04 -- systemctl restart xiaoya-speech xiaoya-agent
pwsh -File deployment/start-wsl.ps1
```

源码删除会同步到运行目录，不能手工复制单个文件留下旧实现。需要更新凭据时，先核对两侧私有配置，再显式执行 `sync-runtime.sh <repo> --configuration`。示例 MCP 默认不接入；开启前配置自己的 `mcp.local.json`。

WSL 的 `bash -lc` 读取登录环境，使用户安装在 `~/.local/bin` 的 uv 可见；本次普通非登录调用实际无法找到 uv，已使用上述入口完成同步。

Agent 的 `ExecStartPre` 等待 speech 最长 300 秒；整体 `TimeoutStartSec=330`。`Type=notify` 仅在 SDK 公开 `worker_registered` 事件后发送 READY；HTTP 200 或旧进程存活不能证明 Agent 已注册。重新启动 speech 后同时重启 Agent。

日常使用的网页可启动生产模式：

```powershell
pwsh -File deployment/start-web.ps1 -Production
```

该入口在 `.next-production-<端口>` 完成生产构建后运行 `next start`，默认只监听 `127.0.0.1:3000`；省略 `-Production` 仍为开发模式。两种模式都读取当前 WSL 地址、核对监听进程归属并按端口保存日志／PID。切换前先结束会话并停止已有网页进程，端口被占用时入口会停止操作。构建失败不会创建新网页进程，也不覆盖开发缓存。

speech 与 Agent 健康接口绑定 `127.0.0.1`。LiveKit 信令与媒体必须保持 Windows 可达，不能一起改成回环。网页启动器读取 WSL eth0 地址，只传入本次子进程环境，不反复改写 `web/.env.local`。启动失败只清理本次创建的网页进程。

## 安全与运维

- 根配置中 STT／TTS 密钥不可留空；无鉴权填 `not-required`。DeepSeek 必须真实独立密钥，不从其他模型配置继承。
- Jev 在根 `.env.local` 使用 `VOICE_AGENT_JEV_BASE_URL=https://api.typesafe.ai`、`VOICE_AGENT_JEV_MODEL=jev-1.13.0` 与独立 `VOICE_AGENT_JEV_API_KEY`；空 Key 保持自然表现，不影响正常对话。填 Key 后用显式配置同步并重启 Agent，密钥不下发网页。Mock 仅用于离线或受管验收，不是部署供应商。
- 2026-10-11 的房间验收临时向同一 Agent 注入 Mock Jev，保留真实私有识别、对话和合成；受管结束恢复正式入口，未改变私有 Key。严格帧观测使用独立开发诊断端口，生产页面另做真实链路检查，两份证据分别记录。
- 本地语音要求 CUDA；`SPEECH_TTS_ACCELERATION` 明确为 `vllm` 或 `none`。保留 CosyVoice 实际需要的 Whisper、Matcha 等依赖。
- speech 的 `SPEECH_TTS_FLOW_ACCELERATION=cuda-graph` 在启动预热中捕获两种声学形状；运行中不扩张缓存，其他形状仍由原模型计算。直接运行未设置时为 `none`，其他值会明确失败。捕获不借用 AMP 临时权重缓存，退出时还原模型与掩码；权重、精度、求解步数及训练块保持不变。关闭图或回滚前先结束会话，重载单元后同时重启 speech／Agent，并确认模型健康与公开注册，详见 [延迟验收及回滚](../docs/voice-latency.md)。
- 默认网页监听本机。令牌只接受浏览器 `POST {}`，服务端决定房间、身份、Agent 与最小权限，15 分钟过期，离房清理窗口为 35 秒。
- 远程网页须经已认证网关／隧道。服务端同时配置 `TOKEN_GATEWAY_ORIGIN`、`TOKEN_GATEWAY_KEY`；网关鉴权后覆盖注入 `x-xiaoya-gateway-key`。必须拒绝客户端伪造的同名头，密钥不能下发浏览器。仅做端口转发不是鉴权。
- LiveKit 成功响应遵循 [官方令牌接口契约](https://docs.livekit.io/frontends/build/authentication/endpoint/)；失败信息统一且不包含密钥，响应禁止缓存。

检查本项目单元与日志：

```powershell
wsl -d Ubuntu-22.04 -- systemctl status xiaoya-livekit xiaoya-speech xiaoya-agent
wsl -d Ubuntu-22.04 -- journalctl -u xiaoya-agent -u xiaoya-speech -n 80 --no-pager
```

不要停止其他服务，不要强制删除调度或使用 SDK 私有字段制造验收通过。

## Jev v2 候选与整体回滚

本次本机交付目录为 `.tools/releases/jev-v2-20261009/`：

- `jev-v2-candidate-source.zip`：同一次工作树快照的前端、Agent、锁文件、部署脚本及现有正式运行资产。新 Editor 候选尚未通过，未混入包内。
- `before-jev-matched-source.zip`：旧网页／资产配上更新前**实际私有 Agent 源码**。私有备份与 Git HEAD 有四个文件不同，已按实际备份重建回滚包，不能直接用 HEAD 假设运行版本。
- `manifest.json`：包及逐文件 SHA-256、基准提交和模型未晋升状态；不含私有配置、缓存或 Key。

这是已实现代码的候选源码包，不冒称正式新模型的完整发布包。解压到独立目录后按原入口 `uv sync --locked`、网页 `pnpm install --frozen-lockfile`／`pnpm build` 检查与启动。填 Key 后显式同步配置，源码更新使用受管清单。

以上两个包是 10 月 9 日的历史快照。10 月 11 日当前部署以提交 `45cd6a9` 的业务源码为基准，另加入生产网页入口；更新前后端快照保存在本机 `/opt/xiaoya/releases/current-20261011-45cd6a9/backend-before.tar.gz`，对应源码 ZIP、生产构建与本轮验收记录在 `.tools/deployment-current-20261011/`。新模型技术验收已通过，但真实 FREE 门尚未完成，当前部署继续使用现行资产。

需要回滚时先结束当前会话，再把 `before-jev-matched-source.zip` 解压到本机忽略的独立目录。使用其中的 `deployment/wsl/sync-runtime.sh <解压目录的WSL绝对路径>` 镜像旧后端，重启 `xiaoya-agent` 并等待公开 worker_registered；网页同时从同一解压目录重新构建和启动，保留原有私有环境配置。旧资产也来自该包。不得只回滚 Agent 或只回滚网页，以免 v1/v2 混用；不对活动工作树执行 reset 或覆盖未提交改动。

本次实际后端更新前的额外备份在 `/opt/xiaoya/releases/jev-v2-20261009-before/agent-source.tar.gz`，同目录保留两次受管同步清单。该备份只含源码与锁文件，不能单独替代配套网页／资产回滚包。模型通过全部制作门后再制作包含正式新资产的统一版本。

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
