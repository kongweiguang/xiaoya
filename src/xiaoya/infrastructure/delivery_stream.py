"""受控表达标记的增量解码，不将模型协议扩展带入领域层。"""

from xiaoya.application.prepare_delivery import PrepareDelivery
from xiaoya.domain.delivery import STYLES, DeliveryIntent, SpeechSegment
from xiaoya.infrastructure.speech_tokenizer import _split


class DeliveryStreamParser:
    """任意 token 边界下仍按既有中文句末提交，标记只影响它之后的句段。"""

    def __init__(self) -> None:
        """每次生成独占缓冲，提前生成取消后不向下一轮转移正文或意图。"""
        self._planner = PrepareDelivery()
        self._intent = DeliveryIntent()
        self._text = ""
        self._header: str | None = None
        self._discard_header = False

    def feed(self, text: str) -> list[SpeechSegment]:
        """仅保留很短的协议头；正常方括号文字保留，损坏控制头不被朗读。"""
        output: list[SpeechSegment] = []
        for character in text:
            if self._discard_header:
                if character in "]\n":
                    self._discard_header = False
                continue
            if self._header is not None:
                if character == "]":
                    header, self._header = self._header, None
                    if self._is_control(header):
                        output.extend(self._flush_text())
                        fields = header.split("|")
                        self._intent = (
                            DeliveryIntent.normalize(*fields)
                            if len(fields) == 2
                            else DeliveryIntent()
                        )
                    else:
                        self._text += f"[{header}]"
                elif character == "\n" or len(self._header) >= 128:
                    header, self._header = self._header, None
                    if self._is_control(header):
                        output.extend(self._flush_text())
                        self._intent = DeliveryIntent()
                        self._discard_header = character != "\n"
                    else:
                        self._text += "[" + header + character
                elif (
                    self._is_control(self._header)
                    and character.isalnum()
                    and not character.isascii()
                ):
                    # 丢失右括号时，以中文正文恢复中性；不能整句吞掉，也不能读出控制头。
                    output.extend(self._flush_text())
                    self._header = None
                    self._intent = DeliveryIntent()
                    self._text += character
                else:
                    self._header += character
                continue
            if character == "[":
                self._header = ""
                continue
            self._text += character
            parts, self._text = _split(self._text)
            for part in parts:
                output.extend(self._emit(part))
        return output

    def finish(self) -> list[SpeechSegment]:
        """正常完成才提交尾句；半个内部头丢弃，普通引用方括号仍属于正文。"""
        if self._header is not None and not self._is_control(self._header):
            self._text += "[" + self._header
        self._header = None
        self._discard_header = False
        return self._flush_text()

    def _flush_text(self) -> list[SpeechSegment]:
        """表达变化也是句段边界，保持变化前的文字使用原有语气。"""
        text, self._text = self._text, ""
        return self._emit(text)

    def _emit(self, text: str) -> list[SpeechSegment]:
        """无新标记的后续句子沿用风格但不重复手势，避免每个标点都点头。"""
        segment = self._planner.prepare(text, self._intent.style, self._intent.gesture)
        if segment is None:
            return []
        self._intent = DeliveryIntent(self._intent.style, "none")
        return [segment]

    @staticmethod
    def _is_control(header: str) -> bool:
        """管道符和已知风格识别协议头，普通如[北京]的引用不能被误删。"""
        return "|" in header or any(header.startswith(style) for style in STYLES)
