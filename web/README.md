# 小芽网页客户端

本项目的中文语音与文字聊天界面，使用 Next.js、React 和 LiveKit。网页固定访问 `/api/token`，连接私有 LiveKit 中名为 `xiaoya` 的 Agent。房间传输助手音频，数字人由浏览器使用 Live2D 渲染，口型跟随助手实际播放的声音；加载或动画失败时显示静态后备形象。

## 本机启动

需要 Node.js 24 和 pnpm。先在项目根目录准备私有服务与配置，详见 [部署说明](../deployment/README.md)。

```powershell
pwsh -File deployment/start-wsl.ps1
# 在另一个终端执行
pwsh -File deployment/start-web.ps1
```

浏览器访问 `http://localhost:3000`。启动脚本读取 WSL 地址并更新 `web/.env.local`；手动执行 `pnpm dev` 前需自行核对地址。

首次准备网页依赖，在 `web/` 中执行：

```powershell
pnpm install --frozen-lockfile
```

复制 `.env.example` 为 `.env.local`，填写与私有 LiveKit 一致的地址、API Key、API Secret 及 `AGENT_NAME=xiaoya`。密钥只在服务端读取，不发送给浏览器。当前令牌接口用于本机开发；对外部署前需接入用户鉴权。

## 目录职责

- `app/`：页面、布局与服务端令牌接口。
- `components/app/`：数字人舞台、对话视图、输入控制和主题。
- `components/agents-ui/`：会话上下文与音频播放。
- `components/ui/`：当前使用的基础按钮。
- `hooks/`：连接、取消、重试与减少动态效果设置。
- `styles/`：主题、布局及响应式样式。
- `public/avatar/`：Live2D 模型、纹理、动作、表情、Core 与静态后备素材。
- `lib/avatar/`：人物渲染、音频分析、口型同步与固定 SDK。

## 开发检查

```powershell
pnpm lint
pnpm format:check
pnpm sdk:build
pnpm test
pnpm exec tsc --noEmit --incremental false
pnpm build
```

开发服务正在运行时，用 `$env:NEXT_DIST_DIR='.next-build'` 运行构建，避免覆盖 `.next` 缓存；构建后移除该环境变量。数字人实现与验收范围见 [Live2D 说明](../docs/live2d/README.md)。

本客户端基于 [LiveKit Agent Starter for React](https://github.com/livekit-examples/agent-starter-react) 适配，保留原项目的 [MIT 许可证](LICENSE)。
