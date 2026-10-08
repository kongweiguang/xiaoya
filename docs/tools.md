# 内置工具与独立 MCP 示例

默认只有五个内置工具：实际时钟、十进制计算、保存／查看／删除本次会话便签。语音和文字使用同一用例。便签仅保存在当前会话内存，不创建提醒，也不跨通话持久化。工具结果必须实际确认，不能把模型的一句回复当成操作成功。

知识库和工单已移到 `examples/mcp-demo`，不由生产入口默认装配。它们返回固定样例，不能描述为真实业务。

```powershell
uv run python examples/mcp-demo/main.py
uv run python examples/mcp-demo/main.py --transport streamable-http
```

第一条为 stdio；第二条只在回环地址提供独立 HTTP 调试。示例不需要模型或 LiveKit 配置。

## 显式启用 MCP

```powershell
Copy-Item mcp.example.json mcp.local.json
```

修改被忽略的 `mcp.local.json` 并设置根 `.env.local`：

```dotenv
VOICE_AGENT_MCP_CONFIG_FILE=mcp.local.json
```

为空则不启用外部服务。`servers` 可声明 stdio、streamable_http 或明确的 sse 协议，不按 URL 猜测。stdio 的 `command: "python"` 使用当前 uv 解释器，`args` 不经 shell 拼接；工作目录和相对路径以配置位置解析，Linux 配置不能使用 Windows 路径。

HTTP 服务使用 `url`；`headers_env` 引用宿主环境变量，真实 Header 不放 JSON。例如 `"headers_env": {"Authorization": "XIAOYA_MCP_AUTH"}`，环境变量值为完整 `Bearer <private-token>`。stdio 用 `env_vars` 传递选定变量，不继承其他模型密钥。

`id` 是模型看到的工具前缀，原始名称仍发给 MCP。`allowed_tools` 省略表示全部，空数组表示无工具，不存在的名称阻止启动。`timeout_seconds` 约束握手、发现和调用。

每次会话先连接并发现工具，失败不发送开场白且回收已连接资源。调用超时返回“结果未确认”；外部写操作不自动重试，语音打断也不保证撤回已发出的操作。模型必须支持流式 Chat Completions function calling，包括分块 `delta.tool_calls`、对应 `tool_call_id` 及执行后的继续回复。

## 验证与扩展

```powershell
uv run python deployment/verify-tools.py
uv run python deployment/verify-tools.py --demo
uv run python deployment/verify-tools-room.py
```

默认检查五个内置工具；显式 `--demo` 才检查两个示例 MCP 工具。第一类不连接房间，房间脚本使用合成语音／固定文字并检查实际执行事件、结果与音频下行，不代表真人设备验收。报告写入 `.tools/logs`。

新增业务先定义 domain 规则，再写 application 用例／端口，最后由 infrastructure 转接 SDK；bootstrap 唯一装配。独立示例沿用同样分层，不把样例业务重新复制回生产代码。
