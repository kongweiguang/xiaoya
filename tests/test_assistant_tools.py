"""覆盖可执行计算、时钟与便签隔离，而不是只检查工具名称存在。"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from livekit.agents.llm import ToolError
from livekit.agents.llm.utils import function_arguments_to_pydantic_model

from xiaoya.application.assistant_tools import AssistantTools
from xiaoya.domain.tools import Note, SessionNotes, calculate
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter


@pytest.mark.parametrize(
    "expression,result",
    [
        ("(128 * 3 + 56) / 4", "110"),
        ("0.1 + 0.2", "0.3"),
        ("-5 + 2", "-3"),
        ("7 % 4", "3"),
        ("1 / 8", "0.125"),
        ("1e30", "1" + "0" * 30),
    ],
)
def test_calculation_uses_decimal_arithmetic(expression: str, result: str) -> None:
    """小数必须符合用户直觉，同时覆盖括号、符号、取余和较大的整数。"""
    assert calculate(expression) == result


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "1/0",
        "True + 1",
        "__import__('os').system('whoami')",
        "[1][0]",
        "x + 1",
        "9 ** 999999",
        "1e101",
        "1e-101",
        "+".join(["1"] * 100),
        "1" * 513,
    ],
)
def test_calculation_rejects_code_and_unbounded_inputs(expression: str) -> None:
    """模型输入是非可信数据，代码、非数值以及资源耗尽输入不能进入执行路径。"""
    with pytest.raises(ValueError):
        calculate(expression)


def test_notes_are_idempotent_and_isolated_between_jobs() -> None:
    """同标题重试更新同一记录，其他通话不能读取这些内容，重复删除结果要准确。"""
    first = AssistantTools(clock=Mock())
    second = AssistantTools(clock=Mock())
    first.save_note(" 待办 ", "带好耳机")
    first.save_note("待办", "带好耳机和充电器")
    assert first.list_notes() == [{"title": "待办", "content": "带好耳机和充电器"}]
    assert second.list_notes() == []
    assert first.delete_note("待办")["deleted"] is True
    assert first.delete_note("待办")["deleted"] is False


def test_note_validation_does_not_mutate_existing_data() -> None:
    """容量和内容检查必须在写入前完成，达到上限仍可以更新原记录。"""
    notes = SessionNotes()
    for index in range(50):
        notes.save(Note(str(index), "演示"))
    with pytest.raises(ValueError, match="50"):
        notes.save(Note("额外", "演示"))
    notes.save(Note("0", "已更新"))
    assert len(notes.list()) == 50
    assert notes.list()[0].content == "已更新"
    with pytest.raises(ValueError, match="标题"):
        Note(" ", "内容")
    with pytest.raises(ValueError, match="内容"):
        Note("标题", "字" * 2001)


def test_time_is_independent_of_host_timezone() -> None:
    """固定 UTC 时间后验证跨日转换，Windows 的系统时区不能改变北京时间的日期。"""
    use_case = AssistantTools(
        clock=Mock(now=Mock(return_value=datetime(2026, 10, 5, 18, tzinfo=UTC)))
    )
    assert use_case.current_time()["datetime"] == "2026-10-06T02:00:00+08:00"
    assert use_case.current_time("UTC")["datetime"] == "2026-10-05T18:00:00+00:00"
    with pytest.raises(ValueError, match="时区"):
        use_case.current_time("not-a-timezone")


async def test_livekit_tools_have_valid_schemas_and_recoverable_errors() -> None:
    """使用 SDK 真实模式构造器，避免只在直接 Python 调用中有效的工具签名。"""
    adapter = AssistantToolAdapter(AssistantTools(clock=Mock()))
    tools = {tool.info.name: tool for tool in adapter.tools()}
    assert set(tools) == {"current_time", "calculate", "save_note", "list_notes", "delete_note"}
    for tool in tools.values():
        assert function_arguments_to_pydantic_model(tool).model_json_schema()["type"] == "object"
    assert (await tools["calculate"](expression="0.1+0.2"))["result"] == "0.3"
    with pytest.raises(ToolError):
        await tools["calculate"](expression="1/0")
    await tools["save_note"](title="待办", content="买耳机")
    assert await tools["list_notes"]() == [{"title": "待办", "content": "买耳机"}]
    assert (await tools["delete_note"](title="待办"))["deleted"] is True
