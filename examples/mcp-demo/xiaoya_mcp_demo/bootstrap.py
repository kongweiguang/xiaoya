"""演示装配独立于生产包和语音服务，不继承私有模型或 LiveKit 配置。"""

from xiaoya_mcp_demo.application import DemoCatalog
from xiaoya_mcp_demo.domain import DemoTicket, KnowledgeArticle
from xiaoya_mcp_demo.server import create_demo_mcp


def prepare_demo_mcp(*, host: str = "127.0.0.1", port: int = 8004):
    """演示目录在装配根注入；不依赖模型服务配置，也不读取用户的私有数据。"""
    catalog = DemoCatalog(
        articles=(
            KnowledgeArticle(
                "小芽的工具",
                "可查时间、做四则运算、保存、查看和删除本次通话便签。",
                ("工具", "能力", "计算", "时间"),
            ),
            KnowledgeArticle(
                "会话便签",
                "便签只在当前通话有效；同标题更新，通话结束后清空，不会创建提醒。",
                ("便签", "记住", "记录", "提醒"),
            ),
            KnowledgeArticle(
                "私有部署",
                "LiveKit、识别与合成使用本地服务；对话使用显式配置的模型，"
                "当前可连接用户授权的 DeepSeek，不自动回退其他公共接口。",
                ("私有", "部署", "隐私", "模型"),
            ),
            KnowledgeArticle(
                "MCP 接入",
                "支持 stdio、Streamable HTTP 和 SSE；stdio 可按通话启动独立示例进程。",
                ("mcp", "协议", "接入", "知识库"),
            ),
        ),
        tickets=(
            DemoTicket(
                "DEMO-001",
                "语音助手接入咨询",
                "已受理",
                "演示客服已记录需求，等待确认私有服务地址。",
            ),
            DemoTicket("DEMO-002", "便签使用咨询", "已完成", "已提供仅在本次通话有效的便签说明。"),
            DemoTicket(
                "DEMO-003", "MCP 接入咨询", "处理中", "正在核对演示 MCP 的工具清单与接口契约。"
            ),
        ),
    )
    return create_demo_mcp(catalog, host=host, port=port)
