"""领域不变量不依赖模型或房间即可验证。"""

from dataclasses import FrozenInstanceError

import pytest

from xiaoya.domain.assistant import AssistantProfile


@pytest.mark.parametrize("field_name", ["instructions", "greeting"])
@pytest.mark.parametrize("value", ["", " \n\t"])
def test_profile_rejects_empty_business_text(field_name: str, value: str) -> None:
    """无效规则必须在业务边界被拒绝，不能等付费模型调用才失败。"""
    with pytest.raises(ValueError):
        AssistantProfile(**{field_name: value})


def test_profile_is_immutable() -> None:
    """同一会话的助手设定不可被其他组件意外改写。"""
    profile = AssistantProfile()
    with pytest.raises(FrozenInstanceError):
        profile.greeting = "被修改的开场白"
