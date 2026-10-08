# 独立 MCP 示例

该服务只提供固定样例知识和模拟工单，不由生产 Agent 自动启动。

在项目根目录执行 `uv run python examples/mcp-demo/main.py` 启动 stdio；
独立 HTTP 调试使用 `uv run python examples/mcp-demo/main.py --transport streamable-http`。

需要在通话中接入时，将 `mcp.example.json` 复制为 `mcp.local.json`，
把 `VOICE_AGENT_MCP_CONFIG_FILE` 显式指向该文件。stdio 路径相对于启动工作目录，
部署环境应填写对应绝对路径。工具返回的 `demo: true` 必须保留，不能描述为真实业务。
