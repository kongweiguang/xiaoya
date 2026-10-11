"""纯正文按完整短句尽早提交；分块方式不改变文字，也没有控制头解析。"""

import re

from xiaoya.domain.delivery import SpeechSegment

_BOUNDARY = re.compile(r"[。！？!?；;]+|(?<!\d)\.(?=\s)|\n+")


class SpeechSegmenter:
    """网络 token 可落在小数或编号中间，保留两位邻文并用游标避免长尾反复扫描。"""

    def __init__(self) -> None:
        """每次生成独占缓冲；取消直接丢弃实例，不在清理阶段补说半句。"""
        self._text: list[str] = []
        self._comma_end: int | None = None

    def feed(self, text: str) -> list[SpeechSegment]:
        """逐字扫描只看新增字符的邻位，让任意网络分块保持相同提交时机。"""
        output: list[SpeechSegment] = []
        for character in text:
            previous_length = len(self._text)
            window_start = max(0, previous_length - 2)
            self._text.append(character)
            window = "".join(self._text[window_start:])
            search_start = max(0, previous_length - 1 - window_start)
            consumed = 0
            for match in _BOUNDARY.finditer(window, search_start):
                end = window_start + match.end()
                output.append(SpeechSegment("".join(self._text[consumed:end])))
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
                output.append(SpeechSegment("".join(self._text[: self._comma_end])))
                self._text = self._text[self._comma_end :]
                self._comma_end = self._next_comma(20)
        return output

    def _next_comma(self, start: int) -> int | None:
        """长句只在第 21 字后的语义停顿分割；这是合成切分策略，不是输入长度限制。"""
        return next(
            (
                index + 1
                for index in range(max(20, start), len(self._text))
                if self._text[index] in "，,：:"
            ),
            None,
        )

    def finish(self) -> list[SpeechSegment]:
        """仅正常结束提交尾句，括号、引号、空白和符号都保留原文。"""
        text = "".join(self._text)
        self._text = []
        self._comma_end = None
        return [SpeechSegment(text)] if text else []
