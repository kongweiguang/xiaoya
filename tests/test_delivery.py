"""纯正文流在任意分块下无损，并保持小数、编号和完整短句的合成时机。"""

import pytest

from xiaoya.domain.delivery import (
    GESTURE_LABELS,
    GESTURES,
    STYLE_LABELS,
    STYLES,
    DeliveryIntent,
    SpeechSegment,
    delivery_capability_summary,
)
from xiaoya.infrastructure import speech_segments
from xiaoya.infrastructure.speech_segments import SpeechSegmenter


@pytest.mark.parametrize(
    "text,expected",
    [
        ("你好。有什么可以帮你？", ["你好。", "有什么可以帮你？"]),
        (
            "1. 温度是3.5度，先检查设备。2. 再重启。",
            ["1. 温度是3.5度，先检查设备。", "2. 再重启。"],
        ),
        ("先检查麦克风，再检查网络。", ["先检查麦克风，再检查网络。"]),
        ("答案是“北京。”", ["答案是“北京。", "”"]),
        ("Read [happy birthday!] exactly. ", ["Read [happy birthday!", "] exactly.", " "]),
        ("a. 1. ٢. ². ３. 温度3.5度。", ["a.", " 1. ٢. ².", " ３. 温度3.5度。"]),
        ("Ready.\u00a0Next.\t尾", ["Ready.", "\u00a0Next.", "\t尾"]),
        ("北京。😊！", ["北京。", "😊！"]),
    ],
)
def test_every_chunk_boundary_preserves_semantic_commits(text, expected):
    """分块不能改变语义切分，小数和编号不能因为收到空格就错误提前合成。"""
    for position in range(len(text) + 1):
        parser = SpeechSegmenter()
        segments = parser.feed(text[:position]) + parser.feed(text[position:]) + parser.finish()
        assert [segment.text for segment in segments] == expected
    parser = SpeechSegmenter()
    segments = [segment for character in text for segment in parser.feed(character)]
    assert [segment.text for segment in segments + parser.finish()] == expected


@pytest.mark.parametrize(
    "text", ["]", "[城区]", "“你好。”", " \n\t😊！", "普通[!\n]引用", "[happy|wave]正文"]
)
def test_all_plain_characters_are_lossless_at_three_splits(text):
    """文本不再承担表现协议，符号和括号始终按原文交付字幕。"""
    for first in range(len(text) + 1):
        for second in range(first, len(text) + 1):
            parser = SpeechSegmenter()
            parts = (
                parser.feed(text[:first])
                + parser.feed(text[first:second])
                + parser.feed(text[second:])
                + parser.finish()
            )
            assert "".join(part.text for part in parts) == text


@pytest.mark.parametrize("comma", ["，", ",", "：", ":"])
def test_long_clauses_reset_cursor_without_reusing_previous_comma(comma):
    """长句切在已有语义停顿，第二句独立计数，不跨句重用已提交游标。"""
    text = "芽" * 20 + comma + "芽" * 20 + comma + "芽" * 20
    for position in range(len(text) + 1):
        parser = SpeechSegmenter()
        committed = parser.feed(text[:position]) + parser.feed(text[position:])
        assert [part.text for part in committed] == ["芽" * 20 + comma] * 2
        assert [part.text for part in parser.finish()] == ["芽" * 20]


@pytest.mark.parametrize("length", [5000, 10000])
def test_unpunctuated_tail_does_linear_work_without_length_limit(monkeypatch, length):
    """实际扫描工作量应线性增长，不靠机器计时或长度限制掩盖长尾性能。"""
    boundary = speech_segments._BOUNDARY
    scanned = []

    class CountedBoundary:
        """仍用生产正则，仅观察输入窗口以确认不重扫整段文本。"""

        def finditer(self, text, start):
            """把必要邻文计入工作量，避免测试制造零成本的假象。"""
            scanned.append(len(text))
            return boundary.finditer(text, start)

    monkeypatch.setattr(speech_segments, "_BOUNDARY", CountedBoundary())
    parser = SpeechSegmenter()
    assert parser.feed("芽" * length) == []
    assert max(scanned) <= 3 and sum(scanned) <= 3 * length
    assert parser.finish() == [SpeechSegment("芽" * length)]


def test_short_sentence_emits_before_end_and_cancel_drops_tail():
    """首个完整短句立即提交，取消通过丢弃实例撤销半句而不补说。"""
    parser = SpeechSegmenter()
    assert parser.feed("北京。还有半句") == [SpeechSegment("北京。")]
    replacement = SpeechSegmenter()
    assert replacement.feed("新回复。") == [SpeechSegment("新回复。")]
    assert replacement.finish() == []


def test_voice_mapping_and_capabilities_share_readonly_catalog():
    """Jev 只能选择已注册能力，有限声音映射保持同一音色；正文不持有意图。"""
    assert {DeliveryIntent(style).voice_preset for style in STYLES} == {
        "neutral",
        "happy",
        "gentle",
    }
    assert DeliveryIntent("concerned").voice_preset == "gentle"
    assert DeliveryIntent("curious").voice_preset == "neutral"
    assert DeliveryIntent.normalize("happy", "execute") == DeliveryIntent()
    with pytest.raises(ValueError):
        DeliveryIntent("invented")
    assert set(STYLE_LABELS) == STYLES and set(GESTURE_LABELS) == GESTURES
    with pytest.raises(TypeError):
        STYLE_LABELS["invented"] = "编造能力"
    summary = delivery_capability_summary()
    assert all(label in summary for label in STYLE_LABELS.values())
    assert all(label in summary for name, label in GESTURE_LABELS.items() if name != "none")
    assert not hasattr(SpeechSegment("你好。"), "intent")
    with pytest.raises(ValueError, match="不能为空"):
        SpeechSegment("")
