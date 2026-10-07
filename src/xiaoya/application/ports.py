"""以业务动作定义会话端口，避免用例绑定具体 SDK。"""

from typing import Protocol

from xiaoya.domain.assistant import AssistantProfile


class VoiceConversation(Protocol):
    """由外层提供语音能力，房间连接和资源回收属于适配器生命周期。"""

    async def start(self, profile: AssistantProfile) -> None:
        """成功返回意味着会话可用，失败应让用例停止后续语音操作。"""
        ...

    async def say(self, text: str) -> None:
        """确定的业务文案直接播报，避免让模型改写必要的开场信息。"""
        ...
