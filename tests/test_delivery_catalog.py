"""跨后端协议、浏览器枚举和正式资源检查人物能力目录。"""

import json
import re
from pathlib import Path

from xiaoya.domain.delivery import GESTURES, STYLES


def test_advertised_presets_exist_in_renderer_protocol_and_formal_assets():
    """模型允许选择的值必须同时被接收和渲染，新增预设不能只改提示词冒充实现。"""
    root = Path(__file__).resolve().parents[1]
    presentation = (root / "web/lib/avatar/presentation.ts").read_text(encoding="utf-8")
    delivery = (root / "web/lib/avatar/delivery.ts").read_text(encoding="utf-8")
    for type_name, variable, expected in (
        ("AvatarStyle", "styles", STYLES),
        ("AvatarGesture", "gestures", GESTURES),
    ):
        definition = re.search(rf"export type {type_name}\s*=\s*(.*?);", presentation, re.S)
        assert definition is not None
        assert frozenset(re.findall(r"'([a-z]+)'", definition[1])) == expected
        accepted = re.search(rf"const {variable} = new Set\(\[(.*?)\]\)", delivery, re.S)
        assert accepted is not None
        assert frozenset(re.findall(r"'([a-z]+)'", accepted[1])) == expected
    directory = root / "web/public/avatar/xiaoya"
    model = json.loads((directory / "xiaoya.model3.json").read_text(encoding="utf-8"))
    resources = {item["Name"]: item["File"] for item in model["FileReferences"]["Expressions"]}
    assert STYLES - {"neutral"} <= resources.keys()
    for name in STYLES - {"neutral"}:
        expression = json.loads((directory / resources[name]).read_text(encoding="utf-8"))
        assert expression["Parameters"]
        assert all(not item["Id"].startswith("ParamMouth") for item in expression["Parameters"])
