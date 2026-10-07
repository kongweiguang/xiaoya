"""把计算与便签的不变量留在领域内，不让模型获得任意代码执行能力。"""

import ast
import operator
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, Underflow, localcontext


def calculate(expression: str) -> str:
    """只解释有限算术节点并使用十进制精度，拒绝函数调用和可能拖垮语音进程的输入。"""
    expression = expression.strip()
    if not expression or len(expression) > 512:
        raise ValueError("请输入不超过 512 个字符的算式")
    try:
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 128:
            raise ValueError("算式太复杂，请拆分计算")
        with localcontext() as context:
            context.prec = 28
            context.Emax = 100
            context.Emin = -100
            context.traps[Underflow] = True
            result = _evaluate(tree.body, expression)
            if not result.is_finite() or abs(result) > Decimal("1e100"):
                raise ValueError("计算结果超出范围")
            rendered = format(result, "f")
            if result == 0:
                return "0"
            return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except (SyntaxError, DecimalException, RecursionError, OverflowError) as error:
        raise ValueError("算式无效或超出范围，请检查除数和数字") from error


def _evaluate(node: ast.AST, expression: str) -> Decimal:
    """白名单递归避免 eval；从原文读取小数，保留 0.1 加 0.2 的十进制语义。"""
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value = Decimal(ast.get_source_segment(expression, node) or "")
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
        value = _evaluate(node.operand, expression)
        value = -value if isinstance(node.op, ast.USub) else value
    elif isinstance(node, ast.BinOp) and type(node.op) in {
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
    }:
        operations = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Mod: operator.mod,
        }
        value = operations[type(node.op)](
            _evaluate(node.left, expression), _evaluate(node.right, expression)
        )
    else:
        raise ValueError("只支持数字、括号以及 +、-、*、/、% 运算")
    if not value.is_finite() or (value and abs(value.adjusted()) > 100):
        raise ValueError("数字或计算结果超出范围")
    return value


@dataclass(frozen=True, slots=True)
class Note:
    """标题是会话内便签的稳定标识，同标题保存代表更新，不积累重复记录。"""

    title: str
    content: str

    def __post_init__(self) -> None:
        """去掉边界空白并约束模型产生的长度，便签不能无限挤占进程与上下文。"""
        for name, limit in (("title", 100), ("content", 2000)):
            value = getattr(self, name).strip()
            if not value or len(value) > limit:
                label = "标题" if name == "title" else "内容"
                raise ValueError(f"便签{label}不能为空且不能超过 {limit} 个字符")
            object.__setattr__(self, name, value)


@dataclass(slots=True)
class SessionNotes:
    """便签只属于一次通话，不落盘，也不与其他房间共享用户内容。"""

    _notes: dict[str, Note] = field(default_factory=dict, init=False, repr=False)

    def save(self, note: Note) -> None:
        """同标题可重复提交；限制新增数量，更新已有便签始终可用。"""
        if note.title not in self._notes and len(self._notes) >= 50:
            raise ValueError("本次通话已有 50 条便签，请先删除不需要的便签")
        self._notes[note.title] = note

    def list(self) -> tuple[Note, ...]:
        """返回不可变快照，外层无法绕过便签校验直接修改聚合。"""
        return tuple(self._notes.values())

    def delete(self, title: str) -> bool:
        """不存在时也返回确定结果，重复删除不会被误报为新完成的操作。"""
        return self._notes.pop(title.strip(), None) is not None
