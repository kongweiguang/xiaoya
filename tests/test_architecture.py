"""把 DDD 和函数注释约束变成离线可执行的边界检查。"""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "xiaoya"
SPEECH_ROOT = Path(__file__).resolve().parents[1] / "services" / "speech" / "src" / "local_speech"
LAYER_RULES = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "infrastructure": {"domain", "application", "infrastructure"},
    "interfaces": {"domain", "application", "interfaces", "bootstrap"},
}


@pytest.mark.parametrize("layer,allowed_layers", LAYER_RULES.items())
def test_imports_respect_ddd_boundaries(layer: str, allowed_layers: set[str]) -> None:
    """检查实际导入目标，防止领域与用例因新功能逐步绑定外部框架。"""
    for path in (PACKAGE_ROOT / layer).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert not node.level, f"{path}: 使用绝对导入，保证分层检查可追踪"
                targets = [node.module or ""]
            for target in targets:
                parts = target.split(".")
                if parts[0] == "xiaoya":
                    target_layer = parts[1] if len(parts) > 1 else ""
                    assert target_layer in allowed_layers, f"{path}: 禁止依赖 {target}"
                elif layer in {"domain", "application"}:
                    assert parts[0] in sys.stdlib_module_names, f"{path}: 禁止外部依赖 {target}"


def test_all_project_functions_have_docstrings() -> None:
    """将设计原因保留在函数附近，新增代码不能绕过项目的注释要求。"""
    paths = (
        list(PACKAGE_ROOT.rglob("*.py"))
        + list(SPEECH_ROOT.glob("*.py"))
        + list(Path(__file__).parent.rglob("*.py"))
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert ast.get_docstring(node), f"{path}:{node.lineno} {node.name} 缺少函数级注释"


@pytest.mark.parametrize(
    "layer,allowed",
    [
        ("domain", {"domain"}),
        ("application", {"domain", "application"}),
        ("infrastructure", {"domain", "infrastructure", "streaming_asr", "acceleration"}),
        ("acceleration", {"acceleration"}),
        ("bootstrap", {"application", "infrastructure", "streaming_asr", "bootstrap"}),
        ("api", {"domain", "application", "bootstrap", "api"}),
    ],
)
def test_local_speech_preserves_ddd_boundaries(layer: str, allowed: set[str]) -> None:
    """新推理服务也须隔离模型库和 HTTP，使部署需求不会侵入领域与应用端口。"""
    path = SPEECH_ROOT / f"{layer}.py"
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, f"{path}: 使用绝对导入"
            targets = [node.module or ""]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            if parts[0] == "local_speech":
                assert parts[1] in allowed, f"{path}: 禁止依赖 {target}"
            elif layer in {"domain", "application"}:
                assert parts[0] in sys.stdlib_module_names, f"{path}: 禁止依赖 {target}"
