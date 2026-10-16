"""只验证送往Jev的阶段与授权合同；离线提示合同不冒充真实语义分类通过。"""

import hashlib
import json

import pytest

from xiaoya.domain.presentation import PresentationContext
from xiaoya.infrastructure.jev import COMMAND_LABELS, JevPresentationDecider


@pytest.mark.parametrize(
    "user",
    [
        "挥挥",
        "小芽，挥挥吧",
        "点一下",
        "他说‘挥挥’",
        "别挥挥",
        "如果叫你挥挥呢",
        "让小明挥挥",
        "挥挥再摇头",
    ],
)
def test_elliptical_requests_preserve_raw_user_and_one_semantic_authority(user: str) -> None:
    """短祈使及危险近邻都交给同一问题；客户端不得补词、读回复或按关键词授命令。"""
    decider = JevPresentationDecider(
        base_url="https://example.invalid", model="fixture", api_key="offline-only"
    )
    context = PresentationContext("thinking", "turn", 1, user_text=user, text="我已经挥手了。")
    payload = decider._user_request(context)
    command = payload["questions"]["requested_gesture"]
    assert payload["state"] == {"user": user}
    assert set(command["criteria"]) == set(COMMAND_LABELS)
    assert "省略宾语祈使句" in command["instructions"]
    assert "五种目录动作中仍唯一可确定" in command["instructions"]
    assert "歧义" in command["criteria"]["none"]
    for boundary in ("否定", "引用", "假设", "他人动作", "复合命令"):
        assert boundary in command["instructions"]
    assert (
        command
        == decider._user_request(
            PresentationContext("thinking", "other-turn", 9, user_text="完全不同的话题")
        )["questions"]["requested_gesture"]
    )
    assert decider._client is None


def test_positive_listening_uses_happy_expression_without_stealing_reply_nod() -> None:
    """只把祝贺认可放到回复阶段；倾听好奇与普通安全事实仍保留原轻反应选项。"""
    questions = JevPresentationDecider._presentation_questions(listening=True)
    gesture = questions["gesture"]
    assert "用户分享积极成果或喜悦时" in gesture["instructions"]
    assert "gesture 选 none" in gesture["instructions"]
    assert "祝贺或认可的点头留给小芽自己的回复" in gesture["instructions"]
    assert "分享积极成果或喜悦" in gesture["criteria"]["none"]
    assert "不用于分享积极成果或喜悦的祝贺认可" in gesture["criteria"]["nod"]
    assert "明确安全的事实或愿望" in gesture["criteria"]["nod"]
    assert "好奇或轻微困惑" in gesture["criteria"]["tilt"]
    assert "倾听用户分享积极成果或喜悦时选 happy" in questions["style"]["instructions"]


def test_reply_question_contract_stays_byte_identical() -> None:
    """本次只调整用户命令及倾听，回复手势、神态、能量完整合同不得顺便变化。"""
    questions = JevPresentationDecider._presentation_questions(listening=False)
    digest = hashlib.sha256(
        json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert digest == "98c2fcf6ff5916144c29a0622c88c77b4580eb0dcda987db4ab85655c6891e65"
