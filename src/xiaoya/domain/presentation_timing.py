"""表现判断的有界时窗属于产品取舍，领域常量避免端口与调度各自猜测超时。"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PresentationTiming:
    """完整输入容纳代理预热，轻回应保持短窗；应用余量仅兜底违反端口约定的实现。"""

    user_total: float = field(default=8.0, init=False)
    reply_total: float = field(default=3.5, init=False)
    listening_total: float = field(default=1.2, init=False)
    application_grace: float = field(default=0.2, init=False)


PRESENTATION_TIMING = PresentationTiming()
