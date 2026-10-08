"""严格内部协议保留用户正文，并在取消、损坏与任意 token 边界下保持意图确定。"""

import pytest

from xiaoya.application.prepare_delivery import PrepareDelivery
from xiaoya.domain.delivery import (
    DELIVERY_INSTRUCTIONS,
    GESTURE_LABELS,
    GESTURES,
    STYLE_LABELS,
    STYLES,
    DeliveryIntent,
    SpeechSegment,
    delivery_capability_summary,
)
from xiaoya.infrastructure import delivery_stream
from xiaoya.infrastructure.delivery_stream import DeliveryStreamParser


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            "[xiaoya:happy|wave]你好呀。[xiaoya:gentle|nod]不用着急。我们慢慢来。",
            [
                ("你好呀。", "happy", "wave"),
                ("不用着急。", "gentle", "nod"),
                ("我们慢慢来。", "gentle", "none"),
            ],
        ),
        ("[xiaoya:unknown|wave]失败了。", [("失败了。", "neutral", "none")]),
        ("[xiaoya:happy|launch]暂不支持。", [("暂不支持。", "neutral", "none")]),
        ("[xiaoya:好奇|歪头]什么想法呀？", [("什么想法呀？", "neutral", "none")]),
        ("[xiaoya: happy|wave]暂不支持。", [("暂不支持。", "neutral", "none")]),
        ("[xiaoya:happy|wave|nod]暂不支持。", [("暂不支持。", "neutral", "none")]),
        ("[happy|wave]普通正文。", [("[happy|wave]普通正文。", "neutral", "none")]),
        (
            "Read [happy birthday] exactly. ",
            [("Read [happy birthday] exactly.", "neutral", "none"), (" ", "neutral", "none")],
        ),
        (
            "[neutrality] [a|b] [城区]温度3.5度。",
            [("[neutrality] [a|b] [城区]温度3.5度。", "neutral", "none")],
        ),
        (
            "[Xiaoya:happy|wave]大小写也是正文。",
            [("[Xiaoya:happy|wave]大小写也是正文。", "neutral", "none")],
        ),
        (
            "[[xiaoya:happy|wave]你好。",
            [("[", "neutral", "none"), ("你好。", "happy", "wave")],
        ),
        (
            "[xiaoya:concerned|none]先说重点[xiaoya:happy|none]再说好消息。",
            [("先说重点", "concerned", "none"), ("再说好消息。", "happy", "none")],
        ),
        (
            "[xiaoya:shy|shy]好呀。继续聊吧。[xiaoya:neutral|shake]换个方法。",
            [
                ("好呀。", "shy", "shy"),
                ("继续聊吧。", "shy", "none"),
                ("换个方法。", "neutral", "shake"),
            ],
        ),
    ],
)
def test_every_chunk_boundary_preserves_text_and_intent(source, expected) -> None:
    """网络分块不能决定语气或误删引用及独立尾部空白；旧协议明确成为普通正文。"""
    for position in range(len(source) + 1):
        parser = DeliveryStreamParser()
        segments = parser.feed(source[:position]) + parser.feed(source[position:]) + parser.finish()
        assert [
            (item.text, item.intent.style, item.intent.gesture) for item in segments
        ] == expected
    parser = DeliveryStreamParser()
    segments = [item for character in source for item in parser.feed(character)] + parser.finish()
    assert [(item.text, item.intent.style, item.intent.gesture) for item in segments] == expected


@pytest.mark.parametrize("tail", ["[", "[x", "[xiaoya", "[xiaoy", "[happy|no"])
def test_unconfirmed_prefix_at_eof_is_plain_text(tail) -> None:
    """没有完整命名空间就不拥有控制权，未闭合的普通引用也要原样保留。"""
    parser = DeliveryStreamParser()
    result = parser.feed("内容" + tail) + parser.finish()
    assert "".join(item.text for item in result) == "内容" + tail


@pytest.mark.parametrize(
    "tail", ["[xiaoya:", "[xiaoya:gentle|no", "[xiaoya:gentle|nod缺右括号正文。"]
)
def test_confirmed_unclosed_header_is_not_guessed_as_body(tail) -> None:
    """确认控制区后宁可丢弃损坏区域，不能猜测中文边界或让内部头被朗读。"""
    parser = DeliveryStreamParser()
    result = parser.feed("之前。" + tail) + parser.finish()
    assert "".join(item.text for item in result) == "之前。"


@pytest.mark.parametrize("delimiter", ["]", "\n"])
def test_long_invalid_header_has_bounded_storage_and_recovers(delimiter) -> None:
    """缓冲上限来自合法枚举，不随着损坏头或模型长输出增长；恢复后必须中性。"""
    parser = DeliveryStreamParser()
    result = parser.feed("[xiaoya:happy|wave]之前。[xiaoya:" + "x" * 10000 + delimiter + "恢复。")
    result += parser.finish()
    assert [(item.text, item.intent) for item in result] == [
        ("之前。", DeliveryIntent("happy", "wave")),
        ("恢复。", DeliveryIntent()),
    ]
    assert parser._header is None and not parser._discard_header


def test_cancelled_generation_does_not_flush_uncommitted_text() -> None:
    """取消直接丢弃每轮解析器，不因清理而把半句变成新的播放请求。"""
    parser = DeliveryStreamParser()
    assert parser.feed("[xiaoya:gentle|nod]已经说出。还有半句") == [
        PrepareDelivery().prepare("已经说出。", "gentle", "nod")
    ]
    replacement = DeliveryStreamParser()
    assert replacement.feed("新回复。") == [
        PrepareDelivery().prepare("新回复。", "neutral", "none")
    ]


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
        ("北京。😊！", ["北京。", "😊！"]),
    ],
)
def test_actual_delivery_path_preserves_chinese_numbers_and_clauses(text, expected) -> None:
    """标点分段只改变边界，闭引号和 emoji 必须保留在正文而不是静默删除。"""
    parser = DeliveryStreamParser()
    result = [item for character in text for item in parser.feed(character)] + parser.finish()
    assert [item.text for item in result] == expected


@pytest.mark.parametrize(
    "source,committed,tail",
    [
        ("芽" * 19 + "," + "芽" * 21, [], ["芽" * 19 + "," + "芽" * 21]),
        ("芽" * 20 + "," + "芽" * 19, [], ["芽" * 20 + "," + "芽" * 19]),
        ("芽" * 20 + "," + "芽" * 20, ["芽" * 20 + ","], ["芽" * 20]),
        ("芽" * 20 + "," + "芽" * 20 + ";", ["芽" * 20 + ",", "芽" * 20 + ";"], []),
        (
            "芽" * 20 + "," + "芽" * 19 + "[xiaoya!",
            ["芽" * 20 + "," + "芽" * 19 + "[xiaoya!"],
            [],
        ),
        (
            "芽" * 20 + "," + "芽" * 19 + "[xiaoya\n尾",
            ["芽" * 20 + "," + "芽" * 19 + "[xiaoya\n"],
            ["尾"],
        ),
        (
            "芽" * 21 + "," + "芽" * 13 + "[xiaoyX!]尾部。",
            ["芽" * 21 + ",", "芽" * 13 + "[xiaoyX!", "]尾部。"],
            [],
        ),
        ("a. 1. ٢. ². ３. 温度3.5度。", ["a.", " 1. ٢. ².", " ３. 温度3.5度。"], []),
        ("Ready.\u00a0Next.\t尾", ["Ready.", "\u00a0Next."], ["\t尾"]),
        ("1. abc. ", ["1. abc."], [" "]),
    ],
)
def test_incremental_clause_scan_preserves_exact_commits_at_every_split(
    source: str, committed: list[str], tail: list[str]
) -> None:
    """固定提交时机而非只拼正文，覆盖普通前缀还原的原子追加、逗号阈值和 Unicode 数字。"""
    for position in range(len(source) + 1):
        parser = DeliveryStreamParser()
        result = parser.feed(source[:position]) + parser.feed(source[position:])
        assert [segment.text for segment in result] == committed
        assert all(segment.intent == DeliveryIntent() for segment in result)
        assert [segment.text for segment in parser.finish()] == tail
    parser = DeliveryStreamParser()
    result = [segment for character in source for segment in parser.feed(character)]
    assert [segment.text for segment in result] == committed
    assert [segment.text for segment in parser.finish()] == tail


@pytest.mark.parametrize("comma", ["，", ",", "：", ":"])
def test_clause_cursor_resets_without_reusing_previous_comma(comma: str) -> None:
    """第二句的逗号位置重新计数，不能沿用已提交句段的游标或漏掉已有四种分句标点。"""
    source = "芽" * 20 + comma + "芽" * 20 + comma + "芽" * 20
    for position in range(len(source) + 1):
        parser = DeliveryStreamParser()
        committed = parser.feed(source[:position]) + parser.feed(source[position:])
        assert [segment.text for segment in committed] == ["芽" * 20 + comma] * 2
        assert [segment.text for segment in parser.finish()] == ["芽" * 20]


@pytest.mark.parametrize("style", sorted(STYLES))
@pytest.mark.parametrize("gesture", sorted(GESTURES))
def test_linear_tail_keeps_all_intents_and_pending_gesture_at_every_split(
    style: str, gesture: str
) -> None:
    """纯符号不消耗手势，逗号提交后不重放动作；全部现有枚举共用相同分块语义。"""
    source = f"[xiaoya:{style}|{gesture}]😊！" + "芽" * 20 + "," + "芽" * 20 + "。尾"
    expected = [
        ("😊！", DeliveryIntent(style, "none")),
        ("芽" * 20 + ",", DeliveryIntent(style, gesture)),
        ("芽" * 20 + "。", DeliveryIntent(style, "none")),
        ("尾", DeliveryIntent(style, "none")),
    ]
    for position in range(len(source) + 1):
        parser = DeliveryStreamParser()
        committed = parser.feed(source[:position]) + parser.feed(source[position:])
        assert [(segment.text, segment.intent) for segment in committed] == expected[:-1]
        result = committed + parser.finish()
        assert [(segment.text, segment.intent) for segment in result] == expected
    parser = DeliveryStreamParser()
    result = [segment for character in source for segment in parser.feed(character)]
    assert [(segment.text, segment.intent) for segment in result] == expected[:-1]
    result.extend(parser.finish())
    assert [(segment.text, segment.intent) for segment in result] == expected


@pytest.mark.parametrize("length", [5000, 10000])
def test_unpunctuated_tail_scans_linear_work_without_timing_limits(
    monkeypatch, length: int
) -> None:
    """用真实正则的输入工作量验证线性增长，不依赖机器速度或用字数上限掩盖长尾问题。"""
    boundary = delivery_stream._BOUNDARY
    scanned: list[int] = []

    class CountedBoundary:
        """只记录实际扫描窗口，仍委托同一个正则识别句末，不复制旧生产解析器。"""

        def finditer(self, text: str, start: int):
            """两位邻文必须计入工作量，避免零成本模拟器制造优化已完成的假象。"""
            scanned.append(len(text))
            return boundary.finditer(text, start)

    monkeypatch.setattr(delivery_stream, "_BOUNDARY", CountedBoundary())
    source = "芽" * length
    parser = DeliveryStreamParser()
    assert parser.feed(source) == []
    assert sum(scanned) <= 3 * length
    assert max(scanned) <= 3
    assert [segment.text for segment in parser.finish()] == [source]


def test_short_sentence_emits_before_generation_finishes() -> None:
    """短句无需等待长文或固定字符门槛，尾句仅在正常结束时提交。"""
    parser = DeliveryStreamParser()
    assert [item.text for item in parser.feed("北京。还有")] == ["北京。"]
    assert [item.text for item in parser.finish()] == ["还有"]


def test_fixed_greeting_and_voice_mapping_share_intent() -> None:
    """固定开场白共用有限声音映射；正文准备保留符号，只有真正空输入不创建句段。"""
    segment = PrepareDelivery().greeting("你好呀。")
    assert segment.intent == DeliveryIntent("happy", "wave")
    assert segment.intent.marker == "[xiaoya:happy|wave]"
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
    assert PrepareDelivery().prepare("😊！", "happy", "wave").text == "😊！"
    assert PrepareDelivery().prepare("", "happy", "wave") is None


@pytest.mark.parametrize(
    "text",
    [
        "[happy birthday!]",
        "Read [happy birthday!]",
        "[!]后面。",
        "普通[neutrality!]正文。",
        "[!\n]",
        "答案是“北京。”",
        'Read [happy birthday!]. "',
        " \n\t😊！",
    ],
)
def test_plain_brackets_quotes_and_whitespace_are_lossless_at_every_split(text: str) -> None:
    """任意两次切分及逐字符输入都必须可重组为原文，不用字符种类判断正文是否有价值。"""
    for first in range(len(text) + 1):
        for second in range(first, len(text) + 1):
            parser = DeliveryStreamParser()
            segments = (
                parser.feed(text[:first])
                + parser.feed(text[first:second])
                + parser.feed(text[second:])
                + parser.finish()
            )
            assert "".join(segment.text for segment in segments) == text
            assert all(segment.intent == DeliveryIntent() for segment in segments)
    parser = DeliveryStreamParser()
    segments = [segment for character in text for segment in parser.feed(character)]
    segments.extend(parser.finish())
    assert "".join(segment.text for segment in segments) == text


def test_symbol_segments_preserve_pending_gesture_until_spoken_body() -> None:
    """保留符号不能让没有音频的片段提前消耗挥手，真正正文后也不能重复触发手势。"""
    parser = DeliveryStreamParser()
    segments = parser.feed("[xiaoya:happy|wave]😊！\n你好。再见。]") + parser.finish()
    assert [(segment.text, segment.intent) for segment in segments] == [
        ("😊！", DeliveryIntent("happy", "none")),
        ("\n", DeliveryIntent("happy", "none")),
        ("你好。", DeliveryIntent("happy", "wave")),
        ("再见。", DeliveryIntent("happy", "none")),
        ("]", DeliveryIntent("happy", "none")),
    ]


@pytest.mark.parametrize("text", ["]", "”", "😊！", " \n"])
def test_domain_keeps_nonempty_literal_segments(text: str) -> None:
    """领域值对象保存文本事实，不承担音频过滤；真正空内容仍拒绝以防无意义操作。"""
    assert SpeechSegment(text).text == text
    with pytest.raises(ValueError, match="不能为空"):
        SpeechSegment("")


def test_prompt_and_capabilities_share_one_readonly_catalog() -> None:
    """唯一生成协议与领域目录共享枚举，历史纯正文不会导致下一轮省略内部头。"""
    assert set(STYLE_LABELS) == STYLES
    assert set(GESTURE_LABELS) == GESTURES
    with pytest.raises(TypeError):
        STYLE_LABELS["invented"] = "编造能力"
    summary = delivery_capability_summary()
    assert all(label in summary for label in STYLE_LABELS.values())
    assert all(label in summary for name, label in GESTURE_LABELS.items() if name != "none")
    assert f"风格只能是{'、'.join(STYLE_LABELS)}" in DELIVERY_INSTRUCTIONS
    assert f"手势只能是{'、'.join(GESTURE_LABELS)}" in DELIVERY_INSTRUCTIONS
    assert "[xiaoya:shy|shy]好呀，有点不好意思呢。" in DELIVERY_INSTRUCTIONS
    assert "仅当当前客户端支持人物表现时" in DELIVERY_INSTRUCTIONS
    assert "不要再询问是否确认" in DELIVERY_INSTRUCTIONS
    assert "console、客户端不支持或本轮没有确认表现能力时" in DELIVERY_INSTRUCTIONS
    assert "嘴部始终跟随实际语音口型" in DELIVERY_INSTRUCTIONS
    assert "叶子仅有常态轻摆，不是语义可控动作" in DELIVERY_INSTRUCTIONS
    assert "shake仅用于明确否定、婉拒或用户明确请求摇头" in DELIVERY_INSTRUCTIONS
