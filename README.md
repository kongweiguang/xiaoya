# 小芽 · Xiaoya

中文语音数字人助手：浏览器连接私有 LiveKit，本地 Paraformer 识别、DeepSeek Flash 对话、本地 CosyVoice 合成，Live2D 表现与实际播放的语音同步。也可以使用文字聊天。

保留薄 DDD 分层，只维护一套灵动 Agent。没有基础／增强开关，没有公共语音服务回退，也没有账号平台。内置时间、计算和保存／查看／删除本次会话便签五个工具；外部 MCP 必须显式配置，知识库与工单仅是独立示例。

## 开始使用

环境：Windows 11、PowerShell 7、Python 3.12、uv、Node.js 24、pnpm 9.15.9。语音 GPU 服务在 WSL Ubuntu 22.04 运行，模型权重不随仓库交付。

```powershell
uv sync --locked
pnpm --dir web install --frozen-lockfile
Copy-Item .env.example .env.local
Copy-Item web/.env.example web/.env.local
```

已有本机配置不要覆盖。根配置显式填写每个服务的地址、模型和独立密钥；无鉴权的私有 STT／TTS 填 `not-required`，不能留空。DeepSeek 固定 `https://api.deepseek.com`、`deepseek-flash`、`thinking.type=disabled`。禁止从 `OPENAI_API_KEY` 继承密钥。

网页配置只在服务端使用 LiveKit Secret。默认仅允许本机同源令牌请求；远程访问需已认证网关，不能直接开放匿名公网入口，见 [部署说明](deployment/README.md)。

准备官方独立 LiveKit CLI（或 `.tools/lk.exe`）：

```powershell
winget install LiveKit.LiveKitCLI
uv run xiaoya download-files
uv run xiaoya console
```

console 使用本机音频，不需要 LiveKit 凭据。开发房间使用 `uv run xiaoya dev`，必须完整填写私有 LiveKit 的 URL、API Key 和 Secret。WSL 常驻运行则只使用 systemd：

```powershell
pwsh -File deployment/start-wsl.ps1
pwsh -File deployment/start-web.ps1
```

网页打开 `http://localhost:3000`，点击开始才创建 Room 和媒体资源。连接可取消，权限拒绝可重试或选择文字入口；草稿与历史不随连接重建清空。结束聊天会等待后端结束确认，超时也会回收本次连接。

## 运行链路与边界

- Agent 固定 WebSocket Paraformer；speech 的 HTTP 转写仅为独立诊断接口，不是回退路径。
- PCM／WAV 共用一条可取消的合成链路。在线程外等待许可，最多等待 30 秒；首块前失败返回 503，首块后失败终止流，不重复播放。
- 会话唯一持有 SDK 客户端和表现快照；启动中途失败释放已创建资源，不发送开场白。正常通话由 Job 生命周期关闭。
- 房间表现使用 `v:1`、SID、修订号、播放许可与结束 ACK。模型内部控制头仅为 `[xiaoya:<style>|<gesture>]`，不会进入语音、字幕或历史；普通括号正文完整保留。
- 麦克风音频在私有 LiveKit 与本地 speech 处理；识别文本、对话历史和工具上下文会发送给 DeepSeek。便签仅在当前会话内存保存。

## 开发验证

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run xiaoya --help
pnpm --dir web test
pnpm --dir web lint
pnpm --dir web typecheck
pnpm --dir web format:check
pnpm --dir web build
```

所有需要角色 SDK 的入口先执行 `sdk:build`。官方 TypeScript 源码、必要补丁、Core 二进制及许可证保留；生成的 JS／类型声明不作为源码维护。测试默认不连接外部模型或真实房间。

代码边界见 [AGENTS.md](AGENTS.md)；工具见 [工具说明](docs/tools.md)；表现与模型见 [灵动交互](docs/delivery.md) 和 [模型资产](assets/avatar/xiaoya/README.md)。本轮结果见 [分层验收记录](docs/delivery-acceptance.md)。旧证据已归档，不代表本轮通过。

当前运行模型在稳定版候选包通过前保留。官方 Editor 往返、完整房间链路和真人麦克风／扬声器是独立验收层，不以离线测试或合成音频替代。
