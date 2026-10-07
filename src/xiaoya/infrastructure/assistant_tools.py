"""LiveKit 工具只转接应用用例，不在 SDK 回调里实现业务规则。"""

from datetime import UTC, datetime
from typing import Annotated

from livekit.agents.llm import ToolError, find_function_tools, function_tool

from xiaoya.application.assistant_tools import AssistantTools


class SystemClock:
    """先取 UTC，再由用例转换地区，消除宿主机时区对结果的隐式影响。"""

    def now(self) -> datetime:
        """带时区时间使 Windows、WSL 和固定时钟测试遵循相同契约。"""
        return datetime.now(UTC)


class AssistantToolAdapter:
    """工具描述面向模型，函数注释保留设计约束；错误通过 SDK 的 ToolError 返回模型。"""

    def __init__(self, use_case: AssistantTools) -> None:
        """依赖已装配的用例，适配器不能自行创建跨 Job 共享的便签状态。"""
        self._use_case = use_case

    def tools(self) -> list:
        """交给 SDK 生成参数模式，语音与文字输入使用同一套工具声明。"""
        return find_function_tools(self)

    @function_tool(description="查询实际日期、时间和星期。默认北京时间，可传 IANA 时区。")
    async def current_time(
        self,
        timezone: Annotated[
            str, "IANA 时区，例如 Asia/Shanghai 或 Europe/London"
        ] = "Asia/Shanghai",
    ) -> dict[str, str]:
        """模型不能根据训练数据猜时间；参数问题转换为可继续对话的工具反馈。"""
        try:
            return self._use_case.current_time(timezone)
        except ValueError as error:
            raise ToolError(str(error)) from None

    @function_tool(description="准确计算四则运算和取余。支持括号、小数、+ - * / %，不支持代码。")
    async def calculate(
        self, expression: Annotated[str, "例如 (128 * 3 + 56) / 4"]
    ) -> dict[str, str]:
        """错误反馈让模型能请求修正算式，禁止以任意 Python 执行补救计算。"""
        try:
            return self._use_case.calculate(expression)
        except ValueError as error:
            raise ToolError(str(error)) from None

    @function_tool(description="按用户要求保存或更新本次通话便签。同标题覆盖，通话结束后清空。")
    async def save_note(
        self,
        title: Annotated[str, "便签标题，1 到 100 字"],
        content: Annotated[str, "用户明确要求记住的内容，1 到 2000 字"],
    ) -> dict[str, str]:
        """写入意图由对话规则约束，实际数据校验与幂等更新委托用例。"""
        try:
            return self._use_case.save_note(title, content)
        except ValueError as error:
            raise ToolError(str(error)) from None

    @function_tool(description="查看本次通话保存的全部便签；空列表表示尚未保存。")
    async def list_notes(self) -> list[dict[str, str]]:
        """会话范围由用例实例保证，模型不能通过额外标识读取其他通话内容。"""
        return self._use_case.list_notes()

    @function_tool(description="按用户明确要求删除指定标题的本次通话便签。")
    async def delete_note(self, title: Annotated[str, "要删除的便签标题"]) -> dict[str, str | bool]:
        """返回实际删除结果，不把重复删除或未找到伪装成新的成功操作。"""
        return self._use_case.delete_note(title)
