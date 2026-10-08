"""只解释带唯一命名空间的表达协议，普通括号内容始终属于正文。"""

import re

from xiaoya.application.prepare_delivery import PrepareDelivery
from xiaoya.domain.delivery import GESTURES, STYLES, DeliveryIntent, SpeechSegment

_PREFIX = "[xiaoya:"
_MAX_HEADER = max(len(style) + len(gesture) + 1 for style in STYLES for gesture in GESTURES)
_BOUNDARY = re.compile(r"[。！？!?；;]+|(?<!\d)\.(?=\s)|\n+")


class DeliveryStreamParser:
    """协议头的缓冲有界；不猜测损坏协议中的正文，避免泄漏控制文字。"""

    def __init__(self) -> None:
        """每轮独占尾部与意图，字符列表和逗号游标避免长句重扫；取消直接丢弃而不补说。"""
        self._planner = PrepareDelivery()
        self._intent = DeliveryIntent()
        self._text: list[str] = []
        self._comma_end: int | None = None
        self._prefix = ""
        self._header: str | None = None
        self._discard_header = False

    def feed(self, text: str) -> list[SpeechSegment]:
        """先确定命名空间再解释枚举，部分前缀不匹配时立即还原普通文本。"""
        output: list[SpeechSegment] = []
        for character in text:
            if self._discard_header:
                if character in "]\n":
                    self._discard_header = False
                continue
            if self._header is not None:
                if character == "]":
                    fields = self._header.split("|")
                    self._intent = (
                        DeliveryIntent.normalize(*fields) if len(fields) == 2 else DeliveryIntent()
                    )
                    self._header = None
                elif character == "\n" or len(self._header) >= _MAX_HEADER:
                    self._header = None
                    self._intent = DeliveryIntent()
                    self._discard_header = character != "\n"
                else:
                    self._header += character
                continue
            if self._prefix:
                candidate = self._prefix + character
                if candidate == _PREFIX:
                    output.extend(self._flush_text())
                    self._prefix = ""
                    self._header = ""
                elif _PREFIX.startswith(candidate):
                    self._prefix = candidate
                else:
                    self._prefix = ""
                    if character == "[":
                        output.extend(self._append(candidate[:-1]))
                        self._prefix = "["
                    else:
                        output.extend(self._append(candidate))
                continue
            if character == "[":
                self._prefix = character
            else:
                output.extend(self._append(character))
        return output

    def finish(self) -> list[SpeechSegment]:
        """只在正常完成提交尾句；确认的半头丢弃，未确认的前缀仍是用户正文。"""
        self._text.extend(self._prefix)
        self._prefix = ""
        if self._header is not None or self._discard_header:
            self._intent = DeliveryIntent()
        self._header = None
        self._discard_header = False
        return self._flush_text()

    def _append(self, text: str) -> list[SpeechSegment]:
        """仅扫描新增字符及句点邻位，先句末后逗号；原文只在真实分句时连接一次。"""
        previous_length = len(self._text)
        window_start = max(0, previous_length - 2)
        self._text.extend(text)
        window = "".join(self._text[window_start:])
        # 保留句点的数字前邻，但从旧尾字开始匹配，不能重新解释已经确认过的编号。
        search_start = max(0, previous_length - 1 - window_start)
        parts: list[str] = []
        consumed = 0
        for match in _BOUNDARY.finditer(window, search_start):
            end = window_start + match.end()
            parts.append("".join(self._text[consumed:end]))
            consumed = end
        if consumed:
            self._text = self._text[consumed:]
            self._comma_end = None
            scan_start = 20
        else:
            scan_start = previous_length
        if self._comma_end is None:
            self._comma_end = self._next_comma(scan_start)
        while len(self._text) > 40 and self._comma_end is not None:
            parts.append("".join(self._text[: self._comma_end]))
            self._text = self._text[self._comma_end :]
            # 残尾只来自本次小片段或原有 40 字切分阈值，不会再遍历未切的长正文。
            self._comma_end = self._next_comma(20)
        return [segment for part in parts for segment in self._emit(part)]

    def _next_comma(self, start: int) -> int | None:
        """保留既有第 21 字起的语义分句位置，不设正文上限；已检查的长尾无需再次扫描。"""
        return next(
            (
                index + 1
                for index in range(max(20, start), len(self._text))
                if self._text[index] in "，,：:"
            ),
            None,
        )

    def _flush_text(self) -> list[SpeechSegment]:
        """风格切换或正常结束才连接剩余正文，同时撤销旧逗号游标，避免跨句反向切分。"""
        text = "".join(self._text)
        self._text = []
        self._comma_end = None
        return self._emit(text)

    def _emit(self, text: str) -> list[SpeechSegment]:
        """符号仍提交正文但不触发表演，也不消耗等待下一个可朗读句段的单次手势。"""
        spoken = any(character.isalnum() for character in text)
        segment = self._planner.prepare(
            text, self._intent.style, self._intent.gesture if spoken else "none"
        )
        if segment is None:
            return []
        if spoken:
            self._intent = DeliveryIntent(self._intent.style, "none")
        return [segment]
