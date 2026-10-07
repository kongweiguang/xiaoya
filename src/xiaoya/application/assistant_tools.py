"""工具用例保持框架无关，语音入口与测试调用同一组业务操作。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xiaoya.domain.tools import Note, SessionNotes, calculate


class Clock(Protocol):
    """真实时间由外层提供，测试可以固定时间而不修改全局系统时钟。"""

    def now(self) -> datetime:
        """必须返回带时区的时间，避免 Windows 与 WSL 的本地时区影响业务结果。"""
        ...


@dataclass(slots=True)
class AssistantTools:
    """每个 Job 独立装配便签；模型与 SDK 的生命周期不侵入应用用例。"""

    clock: Clock
    notes: SessionNotes = field(default_factory=SessionNotes)

    def current_time(self, timezone: str = "Asia/Shanghai") -> dict[str, str]:
        """通过 IANA 时区明确时间含义，无效地区向调用者反馈，不能猜测时区。"""
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("时区无效，请使用 Asia/Shanghai 等 IANA 时区名称") from error
        now = self.clock.now()
        if now.tzinfo is None:
            raise ValueError("时钟必须返回带时区的时间")
        local = now.astimezone(zone)
        return {
            "timezone": timezone,
            "datetime": local.isoformat(),
            "weekday": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[
                local.weekday()
            ],
        }

    def calculate(self, expression: str) -> dict[str, str]:
        """算术结果由领域规则产生，避免把模型的估算当成真实计算。"""
        return {"expression": expression, "result": calculate(expression)}

    def save_note(self, title: str, content: str) -> dict[str, str]:
        """先校验后写入，用同标题覆盖使重复工具调用不会创建重复便签。"""
        note = Note(title, content)
        self.notes.save(note)
        return {"title": note.title, "content": note.content, "scope": "仅本次通话，结束后清空"}

    def list_notes(self) -> list[dict[str, str]]:
        """只展示本用例实例的便签，不能从房间名称或用户输入访问其他会话。"""
        return [{"title": note.title, "content": note.content} for note in self.notes.list()]

    def delete_note(self, title: str) -> dict[str, str | bool]:
        """删除结果区分找到与未找到，让模型不能对不存在的便签声称删除成功。"""
        return {"title": title.strip(), "deleted": self.notes.delete(title)}
