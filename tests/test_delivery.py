"""离线验证模型协议分块、正文纯净和有限表现意图。"""

import pytest

from xiaoya.application.prepare_delivery import PrepareDelivery
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.infrastructure.delivery_stream import DeliveryStreamParser


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            "[happy|wave]你好呀。[gentle|nod]不用着急。我们慢慢来。",
            [
                ("你好呀。", "happy", "wave"),
                ("不用着急。", "gentle", "nod"),
                ("我们慢慢来。", "gentle", "none"),
            ],
        ),
        ("[unknown|wave]操作失败了。", [("操作失败了。", "neutral", "none")]),
        ("[happy|launch]这条指令不支持。", [("这条指令不支持。", "neutral", "none")]),
        ("北京[城区]的温度是3.5度。", [("北京[城区]的温度是3.5度。", "neutral", "none")]),
        (
            "[concerned|none]先说重点[happy|none]再说好消息。",
            [("先说重点", "concerned", "none"), ("再说好消息。", "happy", "none")],
        ),
    ],
)
def test_every_chunk_boundary_preserves_text_and_intent(source, expected) -> None:
    """网络分块不能决定表情或泄漏半个标记，变化前后的文字必须绑定各自意图。"""
    for position in range(len(source) + 1):
        parser = DeliveryStreamParser()
        segments = parser.feed(source[:position]) + parser.feed(source[position:]) + parser.finish()
        assert [
            (segment.text, segment.intent.style, segment.intent.gesture) for segment in segments
        ] == expected


def test_character_stream_emits_before_generation_finishes() -> None:
    """明确句末提交首句，后面尚未完成的正文不拖延已经可以合成的内容。"""
    parser = DeliveryStreamParser()
    emitted = []
    for character in "[gentle|nod]不用担心。后面还有":
        emitted.extend(parser.feed(character))
    assert [(segment.text, segment.intent.gesture) for segment in emitted] == [
        ("不用担心。", "nod")
    ]
    assert parser.finish()[0].text == "后面还有"


def test_incomplete_control_header_is_not_spoken() -> None:
    """生成中断留下的协议头不是正文，不能变成用户听到的英文单词。"""
    parser = DeliveryStreamParser()
    assert parser.feed("[gentle|no") == []
    assert parser.finish() == []
    assert DeliveryStreamParser().feed("你好。") == [
        PrepareDelivery().prepare("你好。", "neutral", "none")
    ]


def test_fixed_greeting_and_voice_mapping_share_intent() -> None:
    """固定文案无需模型，但依然走和回复相同的受控表现及声音映射。"""
    segment = PrepareDelivery().greeting("你好呀。")
    assert segment.intent == DeliveryIntent("happy", "wave")
    assert segment.intent.marker == "[happy|wave]"
    assert DeliveryIntent("concerned").voice_preset == "gentle"
    assert DeliveryIntent("curious").voice_preset == "neutral"
    assert DeliveryIntent.normalize("happy", "execute") == DeliveryIntent()
    with pytest.raises(ValueError):
        DeliveryIntent("invented")


def test_empty_symbols_do_not_trigger_speech_or_gesture() -> None:
    """独立符号不是一次有效发言，不能触发手势或空合成请求。"""
    assert PrepareDelivery().prepare("😊！", "happy", "wave") is None


@pytest.mark.parametrize("cut", range(len("[gentle|nod没关系，我们慢慢来。") + 1))
def test_missing_header_delimiter_keeps_chinese_body_neutral(cut):
    """模型偶尔缺右括号仍须保留有效中文正文，内部头不能被朗读。"""
    parser = DeliveryStreamParser()
    text = "[gentle|nod没关系，我们慢慢来。"
    result = parser.feed(text[:cut]) + parser.feed(text[cut:]) + parser.finish()
    assert "".join(segment.text for segment in result) == "没关系，我们慢慢来。"
    assert all(segment.intent == DeliveryIntent() for segment in result)
