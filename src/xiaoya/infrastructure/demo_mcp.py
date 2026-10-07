"""官方 FastMCP 的工具适配，HTTP 或 stdio 都调用同一应用用例。"""

from mcp.server.fastmcp import FastMCP

from xiaoya.application.demo_catalog import DemoCatalog


def create_demo_mcp(catalog: DemoCatalog, *, host: str, port: int) -> FastMCP:
    """HTTP 默认仅绑定本机；服务无数据库、密钥或用户音频，适合独立验证 MCP 协议。"""
    server = FastMCP("xiaoya", host=host, port=port, stateless_http=True, json_response=True)

    @server.tool(description="搜索演示知识库：工具、便签、私有部署、MCP。空查询列出全部。")
    async def search_knowledge(query: str = "") -> dict:
        """异步协议函数只委托用例，未来增加外部检索不能在此混入业务规则。"""
        return catalog.search_knowledge(query)

    @server.tool(
        description="查询模拟工单 DEMO-001、DEMO-002、DEMO-003 的状态。所有记录都是演示数据。"
    )
    async def get_demo_ticket(ticket_id: str) -> dict:
        """保持 found 与 demo 事实，让语音助手明确区分未找到和真实业务办理结果。"""
        return catalog.get_ticket(ticket_id)

    return server
