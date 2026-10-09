# 小芽网页客户端

本项目的中文语音与文字聊天界面，使用 Next.js、React 和 LiveKit。网页固定访问 `/api/token`，连接私有 LiveKit 中名为 `xiaoya` 的 Agent。房间传输助手音频，数字人由浏览器使用 Live2D 渲染，口型跟随助手实际播放的声音；加载或动画失败时显示静态后备形象。

## 本机启动

需要 Node.js 24 和 pnpm 9.15.9。先准备私有服务与配置，详见 [部署说明](../deployment/README.md)。以下启动器命令在项目根目录执行，不是在 `web/` 中：

```powershell
pwsh -File deployment/start-wsl.ps1
# 在另一个终端执行
pwsh -File deployment/start-web.ps1
```

浏览器访问 `http://127.0.0.1:3000`。启动脚本将当前 WSL 地址传入子进程环境；手动执行 `pnpm dev` 前需自行核对 `.env.local` 中的服务地址。开发和生产网页服务默认只绑定回环地址。

启动器按端口使用 `.next-dev-<端口>` 缓存，并保存 `.tools/logs/web-<端口>.stdout.log`、同名 stderr 日志及 PID。并行诊断其他端口不会覆盖当前网页的缓存或进程记录，且子进程配置不会改写 `.env.local`。

首次准备网页依赖，在 `web/` 中执行：

```powershell
pnpm install --frozen-lockfile
```

仅在 `web/.env.local` 不存在时复制 `web/.env.example`，填写与私有 LiveKit 一致的地址、API Key、API Secret 及 `AGENT_NAME=xiaoya`；不要覆盖已有配置。密钥只在服务端读取，不发送给浏览器。

令牌接口只接收 `POST {}`，检查回环同源访问，并以标准 `201` 响应返回 `server_url`、`participant_token` 等字段。Agent、房间、身份、15 分钟令牌、35 秒离房期限及麦克风／数据权限均由服务端构造，旧 `room_config` 和客户端身份配置会被拒绝。

远程访问必须先通过已认证网关，并同时配置 `TOKEN_GATEWAY_ORIGIN` 与 `TOKEN_GATEWAY_KEY`。网关完成用户鉴权后覆盖注入 `x-xiaoya-gateway-key`，客户端提交的同名头不能透传；网关密钥不发送到浏览器。只配置公开来源不能开放匿名令牌签发。

## 目录职责

- `app/`：页面、布局与服务端令牌接口。
- `components/app/`：数字人舞台、对话视图、输入控制和主题。
- `components/agents-ui/`：会话上下文与音频播放。
- `components/ui/`：当前使用的基础按钮。
- `hooks/`：会话状态订阅、本地表现打断与减少动态效果设置。
- `lib/conversation-controller.ts`：唯一会话控制器及每次连接的媒体资源所有者。
- `lib/delivery-channel.ts`：当前两端 SID 共用的快照就绪与表现同步通道。
- `styles/`：主题、布局及响应式样式。
- `public/avatar/`：Live2D 模型、纹理、动作、表情、Core 与静态后备素材。
- `lib/avatar/`：人物渲染、音频分析、口型同步与固定 SDK。

## 开发检查

```powershell
pnpm lint
pnpm format:check
pnpm test
pnpm typecheck
pnpm build
```

开发服务正在运行时，用独立的 `NEXT_DIST_DIR` 构建并恢复原环境值，具体命令见 [根目录说明](../README.md#开发与验证)，避免覆盖开发缓存。数字人实现与验收范围见 [Live2D 说明](../docs/live2d/README.md)。

开发、测试、类型检查、构建与模型验证会先从 `vendor-source` 生成 Cubism／MotionSync JS 和类型声明；`lib/avatar/vendor/` 是可再生构建产物，不纳入版本管理。角色 GPU 由页面舞台持有，Room 和音频时钟只在用户开始后创建，结束后释放；正常结束和断线重试均保留页面草稿及字幕，不自动重发。麦克风关闭时页面提示文字输入，只有实际有效的采集轨道才提示开口与语音打断。

本客户端基于 [LiveKit Agent Starter for React](https://github.com/livekit-examples/agent-starter-react) 适配，保留原项目的 [MIT 许可证](LICENSE)。
