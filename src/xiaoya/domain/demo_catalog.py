"""演示知识与工单均为固定样例，不能被误读为真实业务系统的数据。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnowledgeArticle:
    """结果携带标题和来源，模型回答时可以明确依据哪条演示知识。"""

    title: str
    content: str
    keywords: tuple[str, ...]

    def matches(self, query: str) -> bool:
        """小型样例采用可解释的关键词匹配；不引入向量库或伪装成完整 RAG。"""
        query = query.strip().casefold()
        return (
            not query
            or any(word.casefold() in query for word in self.keywords)
            or any(word in f"{self.title} {self.content}".casefold() for word in query.split())
        )


@dataclass(frozen=True, slots=True)
class DemoTicket:
    """演示工单保留编号与状态，查询不存在的编号不能编造处理进度。"""

    id: str
    title: str
    status: str
    detail: str
