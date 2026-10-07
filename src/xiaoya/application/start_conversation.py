"""保持会话启动的业务顺序独立于传输协议。"""

from dataclasses import dataclass

from xiaoya.application.ports import VoiceConversation
from xiaoya.domain.assistant import AssistantProfile


@dataclass(slots=True)
class StartVoiceConversation:
    """一个 Job 对应一个用例实例，避免跨会话共享可变的语音状态。"""

    conversation: VoiceConversation
    profile: AssistantProfile

    async def execute(self) -> None:
        """先建立会话再播报；启动异常直接传播，防止产生虚假的成功状态。"""
        await self.conversation.start(self.profile)
        await self.conversation.say(self.profile.greeting)
