"""中文朗读按语义标点切分，避免数字、小数与短标题被通用规则拆开。"""

from __future__ import annotations

import re

from livekit.agents import tokenize

_BOUNDARY = re.compile(r"[。！？!?；;]+|(?<!\d)\.(?=\s)|\n+")


class SpeechSentenceTokenizer(tokenize.SentenceTokenizer):
    """优先保留完整句子；长句只在逗号处分段，绝不按固定字符数截断词语。"""

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        """复用流式边界规则并过滤独立符号，整段输入与逐字输入保持同一朗读边界。"""
        parts, tail = _split(text)
        return parts + ([tail] if any(character.isalnum() for character in tail) else [])

    def stream(self, *, language: str | None = None) -> tokenize.SentenceStream:
        """每次合成独立缓冲，取消和下一轮回复不会共享未完成文本。"""
        return SpeechSentenceStream()


def _split(text: str) -> tuple[list[str], str]:
    """小数和序号不作为句末，独立符号不合成；逗号仅在长句中减小首包等待。"""
    parts: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        part = text[start : match.end()]
        if any(character.isalnum() for character in part):
            parts.append(part)
        start = match.end()
    tail = text[start:]
    while len(tail) > 40:
        comma = re.search(r"[，,：:]", tail[20:])
        if comma is None:
            break
        end = 20 + comma.end()
        parts.append(tail[:end])
        tail = tail[end:]
    return parts, tail


class SpeechSentenceStream(tokenize.SentenceStream):
    """收到明确中文句末即可合成，剩余半句在上游结束时一次性提交。"""

    def __init__(self) -> None:
        """SDK 通道提供取消语义，本地只保留尚未提交的文字。"""
        super().__init__()
        self._buffer = ""

    def push_text(self, text: str) -> None:
        """标点跨 LLM token 边界仍保留原文，小数不会因恰好到达句点而提前提交。"""
        self._check_not_closed()
        parts, self._buffer = _split(self._buffer + text)
        for part in parts:
            self._event_ch.send_nowait(tokenize.TokenData(token=part))

    def flush(self) -> None:
        """仅明确的 SDK flush 才提交半句，正常 token 到达不会制造额外停顿。"""
        self._check_not_closed()
        if any(character.isalnum() for character in self._buffer):
            self._event_ch.send_nowait(tokenize.TokenData(token=self._buffer))
        self._buffer = ""

    def end_input(self) -> None:
        """先发送尾句再关闭通道，避免没有末尾标点的回答被漏读。"""
        self.flush()
        self._do_close()

    async def aclose(self) -> None:
        """打断时丢弃未说出的文本，禁止取消后继续触发新的合成请求。"""
        self._buffer = ""
        self._do_close()
