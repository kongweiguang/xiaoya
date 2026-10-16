"""离线约束分类描述的互斥边界；不把提示合同通过当作真实Jev语义质量证据。"""

import hashlib
import json

import pytest

from xiaoya.domain.delivery import STYLE_LABELS, DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationDecision,
    choose_presentation,
)
from xiaoya.infrastructure.jev import JevPresentationDecider


@pytest.mark.parametrize("listening", [False, True])
def test_current_distress_support_and_gentle_followup_keep_concerned(listening: bool) -> None:
    """温柔语气和追问形式不能与当下关切目的竞争，倾听和回复共用同一语义边界。"""
    style = JevPresentationDecider._presentation_questions(listening=listening)["style"]
    assert "安慰支持、陪伴或温柔追问都选 concerned" in style["instructions"]
    assert "关切目的优先于温柔语气或问句形式" in style["criteria"]["concerned"]


@pytest.mark.parametrize("listening", [False, True])
def test_gentle_excludes_current_distress_without_removing_warm_support(listening: bool) -> None:
    """日常陪伴仍可温柔，但不能与安慰真实困境共享同一分类定义。"""
    criteria = JevPresentationDecider._presentation_questions(listening=listening)["style"][
        "criteria"
    ]
    assert "当前没有真实难过、失落、困难或风险" in criteria["gentle"]
    assert "温暖支持、日常陪伴或回应感谢" in criteria["gentle"]
    assert "不用于安慰当下困境" in criteria["gentle"]


@pytest.mark.parametrize("listening", [False, True])
def test_priority_does_not_turn_quotes_history_or_independent_curiosity_into_distress(
    listening: bool,
) -> None:
    """新增优先级必须仍排除引用、假设与过去话题，保留真正兴趣探索的好奇表情。"""
    instructions = JevPresentationDecider._presentation_questions(listening=listening)["style"][
        "instructions"
    ]
    assert "引语与假设不等于正在发生的事实" in instructions
    assert "其他情况忽略过去字段，不延续过去情绪" in instructions
    assert "独立探索兴趣或细节可好奇，关切式追问仍 concerned" in instructions
    assert "confidence" not in instructions and "高置信" not in instructions


@pytest.mark.parametrize(
    "listening,expected_sha",
    [
        (False, "325a6d10820583e0a3946ec856228b8cf1f2c1b504fd744721083ea7629a9a0b"),
        (True, "c5372cc4aee997d47f501f6b621c6a5889607824d3b03963797e17abb648f8f6"),
    ],
)
def test_style_only_patch_preserves_gesture_energy_and_all_style_labels(
    listening: bool, expected_sha: str
) -> None:
    """仅逆回获批的成果倾听分工，仍核原08完整手势能量摘要，防止顺带改变其它合同。"""
    questions = JevPresentationDecider._presentation_questions(listening=listening)
    untouched = {key: value for key, value in questions.items() if key != "style"}
    if listening:
        gesture = untouched["gesture"]
        added = (
            "用户分享积极成果或喜悦时，以 happy 眉眼倾听，gesture 选 none；"
            "祝贺或认可的点头留给小芽自己的回复。"
        )
        assert gesture["instructions"].count(added) == 1
        gesture["instructions"] = gesture["instructions"].replace(added, "", 1)
        assert gesture["criteria"]["none"].count("、分享积极成果或喜悦") == 1
        gesture["criteria"]["none"] = gesture["criteria"]["none"].replace(
            "、分享积极成果或喜悦", "", 1
        )
        assert gesture["criteria"]["nod"] == (
            "小芽理解明确安全的事实或愿望，适合一次轻点头回应；"
            "不用于分享积极成果或喜悦的祝贺认可；当前主要是在问候、告别或仅致谢时不选此项"
        )
        gesture["criteria"]["nod"] = (
            "小芽理解明确安全的事实、成果或愿望，适合一次轻点头回应；"
            "当前主要是在问候、告别或仅致谢时不选此项"
        )
    actual = hashlib.sha256(
        json.dumps(untouched, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert actual == expected_sha
    assert set(questions["style"]["criteria"]) == set(STYLE_LABELS)
    assert questions["style"]["type"] == "choice"


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (0.43, "neutral"),
        (0.58, "neutral"),
        (0.59, "neutral"),
        (0.649, "neutral"),
        (0.65, "concerned"),
        (0.70, "concerned"),
    ],
)
def test_recorded_low_confidence_remains_rejected(confidence: float, expected: str) -> None:
    """原失败的低分仍须被拒绝；消歧不能通过降低0.65或捏造已采纳神态来过门。"""
    context = PresentationContext("speaking", "turn", 1, reply_id="reply", segment_id="segment")
    decision = PresentationDecision(DeliveryIntent("concerned"), confidence, 1.0, 0.0, 0.1)
    cue = choose_presentation(context, decision)
    assert cue.intent.style == expected
    assert cue.intent.gesture == "none"


@pytest.mark.parametrize(
    "user,assistant,previous_user",
    [
        ("小说里的人说自己很失落。", "你想聊这个角色吗？", ""),
        ("假如有人遇到困难呢？", "可以先了解具体情况。", ""),
        ("谢谢你一直陪我聊天。", "能陪你聊聊，我也很愿意。", ""),
        ("现在说说明天的天气吧。", "我们先看天气信息。", "我刚才挺难过的。"),
    ],
)
def test_semantic_counterexamples_are_sent_verbatim_without_keyword_routing(
    user: str, assistant: str, previous_user: str
) -> None:
    """反例完整交给同一Jev问题判断，适配器不得按悲伤词或上一轮内容硬指定表情。"""
    decider = JevPresentationDecider(
        base_url="https://example.invalid", model="fixture", api_key="offline-only"
    )
    context = PresentationContext(
        "speaking",
        "turn",
        1,
        text=assistant,
        user_text=user,
        reply_id="reply",
        segment_id="segment",
        recent_user_text=previous_user,
    )
    payload = decider._request(context)
    assert payload["state"]["user"] == user
    assert payload["state"]["assistant"] == assistant
    assert payload["state"]["previous_user_message"] == previous_user
    assert payload["questions"] == decider._presentation_questions(listening=False)
    assert decider._client is None
