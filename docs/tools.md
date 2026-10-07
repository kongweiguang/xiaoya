# 工具与 MCP 示例

小芽可以通过语音或网页文字使用同一套工具。内置工具默认启用；MCP 在每次通话开始前连接，发现成功后才启动对话。当前示例共有五个内置工具和两个 MCP 工具。

## 直接体验

安装更新后的依赖：

```powershell
uv sync --locked
```

已有 `.env.local` 的，在其中添加以下配置；新复制的 `.env.example` 已包含此项：

```dotenv
VOICE_AGENT_MCP_CONFIG_FILE=mcp.example.json
```

然后启动 `uv run xiaoya console`，或者按部署说明启动 Agent 和网页。示例 MCP 使用当前 uv 环境的 Python，由每次通话自动启动独立子进程，无需再开一个服务窗口。通话结束会关闭子进程。

可以按顺序说或输入：

| 场景 | 示例话术 | 应有结果 |
| --- | --- | --- |
| 时间 | “现在北京时间几号，几点，星期几？” | 使用实际时钟，明确时区 |
| 计算 | “帮我算一下，一百二十八乘三，再加五十六，最后除以四。” | 工具计算结果为 110 |
| 保存便签 | “帮我记一条便签，标题是出门准备，内容是带耳机和充电器。” | 确认保存，说明仅本次通话有效 |
| 查看便签 | “我刚才记了什么？” | 返回当前通话的便签 |
| 更新便签 | “把出门准备改成带耳机、充电器和雨伞。” | 更新同标题便签 |
| 删除便签 | “删掉出门准备这条便签。” | 根据实际结果确认删除 |
| MCP 知识查询 | “查一下知识库，便签能永久保存吗？” | 根据演示知识说明结束通话后清空 |
| MCP 业务查询 | “查一下演示工单 DEMO-001 的进度。” | 返回“已受理”，明确这是模拟工单 |
| 未找到 | “查一下演示工单 DEMO-999。” | 明确未找到，不编造状态 |
| 参数错误 | “帮我算一除以零。” | 说明算式无法计算，会话可继续 |

便签只在内存保存，不会创建闹钟、提醒或跨通话记忆。计算器支持十进制数字、括号和 `+ - * / %`，精度为 28 位；不执行 Python 代码。工具操作以实际结果为准，不能只靠模型回复判断成功。

## 外部 MCP

复制示例配置：

```powershell
Copy-Item mcp.example.json mcp.local.json
```

将 `.env.local` 中的 `VOICE_AGENT_MCP_CONFIG_FILE` 改为 `mcp.local.json`。该文件已忽略版本管理。配置文件包含 `servers` 数组，可以同时接入多个服务。

HTTP 示例：

```json
{
  "servers": [
    {
      "id": "business",
      "transport": "streamable_http",
      "url": "http://127.0.0.1:8004/mcp",
      "timeout_seconds": 15,
      "allowed_tools": ["search_knowledge", "get_demo_ticket"]
    }
  ]
}
```

在另一个终端启动项目提供的 HTTP 服务：

```powershell
uv run xiaoya mcp --transport streamable-http --host 127.0.0.1 --port 8004
```

此命令不需要 LiveKit、STT、LLM 或 TTS 配置。服务默认只监听本机，提供内置演示数据。接入已有的私有服务时填写它的实际 URL 和工具名称。旧 SSE 服务使用 `transport: "sse"`，填写真实 SSE 地址，不按 URL 后缀猜测协议。

鉴权通过环境变量引用，例如服务条目增加：

```json
"headers_env": {"Authorization": "XIAOYA_MCP_AUTH"}
```

在 `.env.local` 或系统环境中设置完整 Header 值：

```dotenv
XIAOYA_MCP_AUTH="Bearer your-private-token"
```

这里是占位符。真实密钥不放入 JSON、代码或示例文件，不继承模型密钥。

stdio 配置使用 `command` 和 `args`，不通过 Shell 拼接命令；`command: "python"` 解析成当前 uv 环境的解释器。`cwd` 默认是配置文件所在目录，相对路径也从该目录解析。子进程需要凭据时使用 `env_vars`，例如 `{"SERVICE_TOKEN": "XIAOYA_SERVICE_TOKEN"}`，左边是子进程变量名，右边是已设置的宿主环境变量名。

`id` 决定模型看到的名称前缀，例如 `business__search_knowledge`；真正发送给 MCP 的名称仍为 `search_knowledge`。不同服务可以提供同名工具。`allowed_tools` 使用服务原始名称；省略表示提供全部工具，空数组表示不提供任何工具，声明了不存在的工具会阻止启动。

`timeout_seconds` 同时限制握手、工具发现和单次调用。服务连接或配置失败会阻止开场白，并回收已建立的连接。调用超时或传输中断时，模型会收到“结果未确认”的反馈；外部写操作不会自动重试。用户打断说话并不保证已经发给外部系统的操作被撤回。

不使用外部工具时，把 `VOICE_AGENT_MCP_CONFIG_FILE` 设置为空；五个内置工具仍可使用。

## 私有模型兼容性

模型必须支持 Chat Completions function calling，而不仅是 SSE 文本：

1. 接收 `tools` 的 JSON Schema。
2. 流式返回 `delta.tool_calls`，工具调用 ID 与 JSON 参数可跨块传输。
3. 接收带 `tool_call_id` 的 `role=tool` 消息，并根据结果继续回复。

工具只在 SDK 确认轮次并授权执行后运行；保留现有提前生成与提前合成，不会在未说完时执行预生成的写操作。工具返回内容作为数据处理，不作为修改助手规则的指令。自定义 `VOICE_AGENT_INSTRUCTIONS` 时也应保留“不编造成功、演示数据要说明、结果未知不自动重试写操作”等要求。

离线验证：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run xiaoya --help
uv run xiaoya mcp --help
```

测试覆盖实际 SDK 的工具参数模式、分片 SSE 工具调用、结果回传和继续回复；HTTP MCP 通过内存 MockTransport 验证，stdio MCP 使用真实本地子进程。默认测试不访问外部模型、MCP 或真实 LiveKit 房间。

填写 `.env.local` 后，可以单独验证当前私有 LLM 的七种工具调用与继续回复：

```powershell
uv run python deployment/verify-tools.py
```

此脚本固定使用项目的 stdio 演示 MCP，便签也是合成测试内容；不连接 LiveKit 房间，不采集音频。证据写入 `.tools/logs/tool-verification.json`。它验证模型与示例工具的兼容性，自己的外部 MCP 仍需单独验证。

已启动私有 LiveKit 和本项目 Agent 后，可以验证实际房间中的工具执行及语音链路：

```powershell
uv run python deployment/verify-tools-room.py
```

脚本创建独立测试房间，向虚拟麦克风注入四轮合成语音，再发送三轮固定文字；七项工具均检查实际执行事件、返回结果和非静音的语音下行，语音输入还检查 STT 转写。结束后删除自己的测试房间，不保存音频。证据写入 `.tools/logs/mcp-room-verification.json`；这不包含真人麦克风和扬声器验收。WSL 运行时用服务环境的 Python，并通过 `--env /opt/xiaoya/agent/.env.local` 指定配置。

## 扩展业务与 WSL 部署

新增本地工具时，先在 `domain` 定义规则，再在 `application` 增加用例，最后由 `infrastructure/assistant_tools.py` 转接 SDK 并在 `bootstrap.py` 装配。示例知识库和工单也沿用同样分层；接数据库或外部业务 API 时新增应用端口和基础设施实现，不在工具函数里直接写 SQL。

WSL 的 `deploy.sh` 和 `sync-agent.sh` 会同步 `mcp.example.json` 以及存在的 `mcp.local.json`。Linux 环境使用自身 Python 与依赖；示例的 `command: "python"` 无需修改。启用 MCP 还须在 `/opt/xiaoya/agent/.env.local` 设置对应文件名。自定义 Windows 路径或进程命令需转换成 Linux 可用配置。

离线检查、私有模型工具验证、完整房间语音链路和真人麦克风/扬声器验收是不同层级。已有数字人的历史房间证据不能作为本次工具功能的验收证据。

官方参考：[LiveKit 工具](https://docs.livekit.io/agents/logic/tools/)与 [MCP 接入](https://docs.livekit.io/agents/logic/tools/mcp/)。本项目使用已安装稳定 SDK 的 `function_tool`、`MCPToolset` 和 MCP 客户端，不使用已弃用的 `mcp_servers` 参数。
