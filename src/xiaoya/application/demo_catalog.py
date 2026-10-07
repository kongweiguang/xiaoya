"""MCP 示例服务复用应用用例，协议入口不直接拼装业务查询逻辑。"""

from dataclasses import dataclass

from xiaoya.domain.demo_catalog import DemoTicket, KnowledgeArticle


@dataclass(frozen=True, slots=True)
class DemoCatalog:
    """示例数据通过装配根注入，后续接数据库时只替换数据获取端口。"""

    articles: tuple[KnowledgeArticle, ...]
    tickets: tuple[DemoTicket, ...]

    def search_knowledge(self, query: str) -> dict:
        """空查询展示全部主题，未命中返回空列表，并始终标明数据只是演示样例。"""
        return {
            "source": "xiaoya 内置演示知识库",
            "demo": True,
            "articles": [
                {"title": item.title, "content": item.content}
                for item in self.articles
                if item.matches(query)
            ],
        }

    def get_ticket(self, ticket_id: str) -> dict:
        """未找到时返回明确状态，避免把模拟编号映射到真实的用户工单。"""
        ticket = next((item for item in self.tickets if item.id == ticket_id.strip().upper()), None)
        result = {"demo": True, "source": "xiaoya 演示工单", "found": ticket is not None}
        if ticket is not None:
            result["ticket"] = {
                "id": ticket.id,
                "title": ticket.title,
                "status": ticket.status,
                "detail": ticket.detail,
            }
        return result
