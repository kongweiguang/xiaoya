"""用固定中文样例验证真实 Jev 增强，默认只检查数据，不自动消费接口额度。"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from xiaoya.domain.delivery import GESTURES, STYLES
from xiaoya.domain.presentation import (
    PHASES,
    PresentationContext,
    PresentationDecision,
    choose_presentation,
)
from xiaoya.infrastructure.jev import JevPresentationDecider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "tests" / "data" / "jev-chinese-cases.json"
PINNED_MODEL = "jev-1.13.0"
CRITICAL_CATEGORIES = frozenset({"negation", "quotation", "hypothetical", "unsupported"})
REQUIRED_CATEGORIES = CRITICAL_CATEGORIES | {"positive", "emotion", "listening", "thinking"}


def read_cases(path: Path) -> list[dict[str, Any]]:
    """先校验固定标签，防止标注错误或基线预置手势虚增线上采纳率。"""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != 1 or document.get("language") != "zh-CN":
        raise ValueError("样例必须使用 v1 中文验收格式")
    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) < 100:
        raise ValueError("至少需要 100 条固定中文样例")
    seen: set[str] = set()
    categories: set[str] = set()
    positive_gestures: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("样例必须是对象")
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("样例 ID 不能为空或重复")
        seen.add(identifier)
        category = case.get("category")
        if category not in REQUIRED_CATEGORIES or case.get("phase") not in PHASES:
            raise ValueError(f"{identifier}: 样例类别或阶段非法")
        categories.add(category)
        for key in ("user_text", "assistant_text", "rationale"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"{identifier}: {key} 必须是非空文本")
        for key, allowed in (("allowed_styles", STYLES), ("allowed_gestures", GESTURES)):
            values = case.get(key)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{identifier}: {key} 必须是受支持的非空标签集合")
        if category in CRITICAL_CATEGORIES and case["allowed_gestures"] != ["none"]:
            raise ValueError(f"{identifier}: 关键反例必须禁止额外手势")
        if category == "positive":
            if case["phase"] != "speaking" or "none" in case["allowed_gestures"]:
                raise ValueError(f"{identifier}: 正向动作必须实际说话并明确要求动作")
            positive_gestures.update(case["allowed_gestures"])
        if case["phase"] != "speaking" and case["allowed_gestures"] != ["none"]:
            raise ValueError(f"{identifier}: 倾听和思考不执行大幅手势")
    if categories != REQUIRED_CATEGORIES or positive_gestures != GESTURES - {"none"}:
        raise ValueError("类别与五种正向动作必须全部覆盖")
    return cases


def make_context(case: dict[str, Any], revision: int) -> PresentationContext:
    """输入不预置风格或手势，表现只能来自 Jev 决策及确定的播放阶段。"""
    speaking = case["phase"] == "speaking"
    return PresentationContext(
        phase=case["phase"],
        turn_id=case["id"],
        revision=revision,
        text=case["assistant_text"] if speaking else case["user_text"],
        user_text=case["user_text"],
        reply_id=f"reply:{case['id']}" if speaking else "",
        segment_id=f"segment:{case['id']}" if speaking else "",
    )


def assess_decision(
    case: dict[str, Any], context: PresentationContext, decision: PresentationDecision | None
) -> dict[str, Any]:
    """同时报告原始采纳与最终仲裁，低覆盖不能靠拒绝全部判断伪装为高精度。"""
    if decision is None:
        return {
            "id": case["id"],
            "category": case["category"],
            "accepted": False,
            "correct": False,
            "style_adopted": False,
            "gesture_adopted": False,
            "positive_hit": False,
            "critical_misfire": False,
            "degraded": True,
        }
    cue = choose_presentation(context, decision)
    style_adopted = decision.style_confidence >= 0.65 and cue.intent.style == decision.intent.style
    gesture_adopted = cue.intent.gesture != "none"
    style_correct = decision.intent.style in case["allowed_styles"]
    gesture_correct = cue.intent.gesture in case["allowed_gestures"]
    accepted = style_adopted or gesture_adopted
    return {
        "id": case["id"],
        "category": case["category"],
        "phase": context.phase,
        "accepted": accepted,
        "correct": (
            accepted
            and (not style_adopted or style_correct)
            and (not gesture_adopted or gesture_correct)
        ),
        "style_adopted": style_adopted,
        "style_correct": style_correct,
        "gesture_adopted": gesture_adopted,
        "gesture_correct": gesture_correct,
        "positive_hit": case["category"] == "positive" and gesture_adopted and gesture_correct,
        "critical_misfire": case["category"] in CRITICAL_CATEGORIES and gesture_adopted,
        "degraded": False,
        "raw_style": decision.intent.style,
        "raw_gesture": decision.intent.gesture,
        "style": cue.intent.style,
        "gesture": cue.intent.gesture,
        "style_confidence": decision.style_confidence,
        "gesture_confidence": decision.gesture_confidence,
        "perform_probability": decision.perform_probability,
        "energy": cue.energy,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    """线性插值处理小样本，同时让无成功请求明确表现为缺失值。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


def ratio(numerator: int, denominator: int) -> float | None:
    """零采纳不能得到满分，用 null 表示无样本并让验收门失败。"""
    return round(numerator / denominator, 4) if denominator else None


def summarize(rows: list[dict[str, Any]], latencies: list[float]) -> dict[str, Any]:
    """固定门槛只针对真实完整跑数，精度、覆盖、关键误动作分别验收。"""
    accepted = [row for row in rows if row["accepted"]]
    styles = [row for row in rows if row["style_adopted"]]
    gestures = [row for row in rows if row["gesture_adopted"]]
    positives = [row for row in rows if row["category"] == "positive"]
    degraded = sum(row["degraded"] for row in rows)
    precision = ratio(sum(row["correct"] for row in accepted), len(accepted))
    positive_rate = ratio(sum(row["positive_hit"] for row in positives), len(positives))
    critical_misfires = sum(row["critical_misfire"] for row in rows)
    return {
        "total_cases": len(rows),
        "categories": dict(sorted(Counter(row["category"] for row in rows).items())),
        "accepted_decisions": len(accepted),
        "accepted_precision": precision,
        "decision_coverage": ratio(len(accepted), len(rows)),
        "style_precision": ratio(sum(row["style_correct"] for row in styles), len(styles)),
        "gesture_precision": ratio(sum(row["gesture_correct"] for row in gestures), len(gestures)),
        "positive_gesture_cases": len(positives),
        "positive_gesture_coverage": positive_rate,
        "critical_misfires": critical_misfires,
        "degraded_cases": degraded,
        "successful_network_requests": len(latencies),
        "network_latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
        },
        "gate_passed": (
            len(rows) >= 100
            and degraded == 0
            and precision is not None
            and precision >= 0.95
            and positive_rate is not None
            and positive_rate >= 0.80
            and critical_misfires == 0
        ),
    }


def mock_response(case: dict[str, Any]) -> dict[str, Any]:
    """按照人工标签构造官方响应契约，只证明解析和仲裁，绝不伪称模型理解能力。"""
    style, gesture = case["allowed_styles"][0], case["allowed_gestures"][0]
    return {
        "model": PINNED_MODEL,
        "answers": {
            "style": {
                "type": "choice",
                "choice": style,
                "probabilities": {option: float(option == style) for option in STYLES},
                "confidence": 1.0,
            },
            "gesture": {
                "type": "choice",
                "choice": gesture,
                "probabilities": {option: float(option == gesture) for option in GESTURES},
                "confidence": 1.0,
            },
            "perform": {"type": "noul", "noul": float(gesture != "none")},
            "energy": {
                "type": "score",
                "score": 1.0,
                "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0},
                "legend": {"0": "Quiet, comforting", "1": "Natural", "2": "Excited"},
                "confidence": 1.0,
            },
        },
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def evaluate_mock(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """固定 Mock 响应必须经过真实解析与仲裁，不读取密钥，不发出任何 HTTP 请求。"""
    rows = []
    for revision, case in enumerate(cases, 1):
        context = make_context(case, revision)
        decision = JevPresentationDecider._parse(mock_response(case))
        rows.append(assess_decision(case, context, decision))
    return {
        "mode": "mock-contract",
        "model": PINNED_MODEL,
        "online_acceptance": "未执行；Mock 只证明契约和仲裁，不代表真实 Jev 中文准确率",
        "summary": summarize(rows, []),
        "cases": rows,
    }


def read_live_configuration(env_file: Path) -> tuple[str, str, str]:
    """显式 live 才读取凭据；验收固定官方站点与模型，避免误把其他服务结果当成 Jev。"""
    load_dotenv(env_file, override=False)
    key = os.environ.get("VOICE_AGENT_JEV_API_KEY", "").strip()
    if not key:
        raise ValueError("请先在 .env.local 填写 VOICE_AGENT_JEV_API_KEY")
    base_url = os.environ.get("VOICE_AGENT_JEV_BASE_URL", "https://api.typesafe.ai").strip()
    model = os.environ.get("VOICE_AGENT_JEV_MODEL", PINNED_MODEL).strip()
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.typesafe.ai"
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("线上验收必须使用官方 https://api.typesafe.ai 根地址")
    if model != PINNED_MODEL:
        raise ValueError(f"线上验收必须固定模型 {PINNED_MODEL}")
    return base_url, model, key


async def evaluate(
    cases: list[dict[str, Any]], base_url: str, model: str, key: str
) -> dict[str, Any]:
    """逐条固定样例共享正式客户端，记录真实网络耗时；报告只含 ID、分数及标签。"""
    decider = JevPresentationDecider(base_url=base_url, model=model, api_key=key)
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    try:
        for revision, case in enumerate(cases, 1):
            context = make_context(case, revision)
            started = time.perf_counter()
            try:
                async with asyncio.timeout(0.7 if context.phase == "listening" else 0.9):
                    decision = await decider.decide(context)
            except TimeoutError:
                decision = None
            elapsed = (time.perf_counter() - started) * 1000
            row = assess_decision(case, context, decision)
            if decision is not None:
                row["network_latency_ms"] = round(elapsed, 2)
                latencies.append(elapsed)
            rows.append(row)
            print(f"{revision}/{len(cases)} {case['id']}", file=sys.stderr)
    finally:
        await decider.aclose()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "mode": "live",
        "limits": {
            "style_confidence": 0.65,
            "gesture_confidence": 0.85,
            "perform_probability": 0.90,
            "accepted_precision": 0.95,
            "positive_gesture_coverage": 0.80,
            "critical_misfires": 0,
        },
        "summary": summarize(rows, latencies),
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    """默认执行可重复离线校验；线上请求必须由用户显式添加 --live。"""
    parser = argparse.ArgumentParser(description="小芽 Jev 中文验收；默认离线，--live 才联网")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true", help="离线验证 138 条 Mock 响应契约与仲裁")
    modes.add_argument(
        "--live", action="store_true", help="显式发送固定样例到官方 Jev，消耗接口额度"
    )
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--output", type=Path, help="保存脱敏 JSON 报告；默认仅打印")
    args = parser.parse_args(argv)
    try:
        cases = read_cases(args.cases)
        if args.live:
            base_url, model, key = read_live_configuration(args.env_file)
            report = asyncio.run(evaluate(cases, base_url, model, key))
        elif args.mock:
            report = evaluate_mock(cases)
        else:
            report = {
                "mode": "offline-validation",
                "total_cases": len(cases),
                "categories": dict(sorted(Counter(case["category"] for case in cases).items())),
                "online_acceptance": "未执行；请填写 Key 后显式运行 --live",
            }
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0 if not (args.live or args.mock) or report["summary"]["gate_passed"] else 1
    except (OSError, ValueError, TypeError, KeyError) as error:
        # 异常原文可能携带第三方响应或配置，CLI 只输出可控类别。
        print(f"Jev 验收未完成：{type(error).__name__}；请检查配置或样例文件", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
