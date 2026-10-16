"""中文样例分别验收用户许可与自然沟通，默认离线且不消费接口额度。"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from xiaoya.domain.delivery import GESTURES, STYLES, DeliveryIntent
from xiaoya.domain.presentation import (
    GESTURE_SOURCES,
    PHASES,
    PresentationContext,
    PresentationDecision,
    UserPresentationDecision,
    choose_presentation,
    choose_user_command,
    choose_user_prohibition,
)
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING
from xiaoya.infrastructure.jev import JEV_RESPONSE_MODEL, JevPresentationDecider
from xiaoya.infrastructure.settings import (
    JEV_BASE_URL,
    JEV_MODEL,
    JEV_PROXY_URL,
    validate_jev_configuration,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "tests" / "data" / "jev-chinese-cases.json"
PINNED_MODEL = JEV_MODEL
CRITICAL_CATEGORIES = frozenset({"negation", "quotation", "hypothetical", "unsupported"})
REQUIRED_CATEGORIES = CRITICAL_CATEGORIES | {
    "positive",
    "emotion",
    "listening",
    "thinking",
    "natural",
}


def read_cases(path: Path) -> list[dict[str, Any]]:
    """来源和用户许可分别标注，合理纠正不等于引用命令误执行，旧反例身份必须保留。"""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") != 2 or document.get("language") != "zh-CN":
        raise ValueError("样例必须使用v2中文验收格式")
    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) < 100:
        raise ValueError("至少需要100条固定中文样例")
    seen, categories, command_gestures, natural_gestures = set(), set(), set(), set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("样例必须是对象")
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("样例ID不能为空或重复")
        seen.add(identifier)
        category = case.get("category")
        if category not in REQUIRED_CATEGORIES or case.get("phase") not in PHASES:
            raise ValueError(f"{identifier}: 样例类别或阶段非法")
        categories.add(category)
        for key in ("user_text", "assistant_text", "rationale"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"{identifier}: {key}必须是非空文本")
        for key, allowed in (
            ("allowed_styles", STYLES),
            ("allowed_gestures", GESTURES),
            ("allowed_sources", GESTURE_SOURCES),
            ("allowed_commands", GESTURES),
            ("allowed_prohibited", GESTURES | {"all"}),
        ):
            values = case.get(key)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{identifier}: {key}必须是受支持的非空标签集合")
        if "none" not in case["allowed_commands"]:
            command_gestures.update(case["allowed_commands"])
            if case["allowed_sources"] != ["command"]:
                raise ValueError(f"{identifier}: 有效指令必须独占本轮动作来源")
        if category == "natural" and "none" not in case["allowed_gestures"]:
            natural_gestures.update(case["allowed_gestures"])
            if case["allowed_sources"] != ["dialogue"] or case["allowed_commands"] != ["none"]:
                raise ValueError(f"{identifier}: 自然动作不能预置指令")
        if case["phase"] == "thinking" and case["allowed_gestures"] != ["none"]:
            raise ValueError(f"{identifier}: 思考阶段不执行自然主动作")
    if (
        categories != REQUIRED_CATEGORIES
        or command_gestures != GESTURES - {"none"}
        or natural_gestures != GESTURES - {"none"}
    ):
        raise ValueError("类别、五种指令和五种自然动作必须全部覆盖")
    return cases


def make_context(case: dict[str, Any], revision: int) -> PresentationContext:
    """输入不预置动作，最近一轮只在样例明确提供时使用，不替模型作语义决定。"""
    speaking = case["phase"] == "speaking"
    return PresentationContext(
        phase=case["phase"],
        turn_id=case["id"],
        revision=revision,
        text=case["assistant_text"] if speaking else case["user_text"],
        user_text=case["user_text"],
        reply_id=f"reply:{case['id']}" if speaking else "",
        segment_id=f"segment:{case['id']}" if speaking else "",
        recent_user_text=case.get("recent_user_text", ""),
        recent_assistant_text=case.get("recent_assistant_text", ""),
        last_command=case.get("last_command", "none"),
    )


def make_user_context(context: PresentationContext) -> PresentationContext:
    """用户输入单独判断，回复不能污染命令许可或本轮禁止要求。"""
    return replace(
        context,
        phase="listening" if context.phase == "listening" else "thinking",
        text=context.user_text,
        reply_id="",
        segment_id="",
        reply_preview=False,
    )


def assess_decision(
    case: dict[str, Any],
    context: PresentationContext,
    decision: PresentationDecision | None,
    user_decision: UserPresentationDecision | None = None,
) -> dict[str, Any]:
    """按正式阈值独立计分，回复降级保留有效命令，且不伪造缺失回复的表情。"""
    row: dict[str, Any] = {
        "id": case["id"],
        "category": case["category"],
        "phase": context.phase,
        "command_case": "none" not in case["allowed_commands"],
        "natural_case": case["category"] == "natural" and "none" not in case["allowed_gestures"],
        "accepted": False,
        "correct": False,
        "style_adopted": False,
        "gesture_adopted": False,
        "command_hit": False,
        "natural_hit": False,
        "critical_misfire": False,
        "degraded": user_decision is None or decision is None,
        "user_degraded": user_decision is None,
        "reply_degraded": decision is None,
    }
    if user_decision is None:
        return row
    reply_available = decision is not None
    # 空回复只用于构造中性结果，零置信度不能被统计为有效表情或自然动作。
    actual_decision = decision or PresentationDecision(DeliveryIntent(), 0, 0, 0, 0.5)
    prohibited = choose_user_prohibition(user_decision)
    command = choose_user_command(user_decision, context.last_command)
    cue = choose_presentation(context, actual_decision)
    gesture, source = cue.intent.gesture, cue.gesture_source
    if command != "none":
        gesture, source = command, "command"
    elif prohibited == "all" or gesture == prohibited:
        gesture, source = "none", "none"
    style_adopted = (
        reply_available
        and actual_decision.style_confidence >= 0.65
        and cue.intent.style == actual_decision.intent.style
    )
    style_correct = actual_decision.intent.style in case["allowed_styles"]
    command_correct, prohibition_correct = (
        command in case["allowed_commands"],
        prohibited in case["allowed_prohibited"],
    )
    gesture_adopted = gesture != "none"
    gesture_correct = gesture in case["allowed_gestures"] and source in case["allowed_sources"]
    expected_prohibited = set(case["allowed_prohibited"]) - {"none"}
    prohibited_violation = gesture_adopted and (
        "all" in expected_prohibited or gesture in expected_prohibited
    )
    accepted = style_adopted or gesture_adopted
    row.update(
        accepted=accepted,
        correct=(
            accepted
            and (not style_adopted or style_correct)
            and (not gesture_adopted or gesture_correct)
            and (command == "none" or command_correct)
            and prohibition_correct
        ),
        style_adopted=style_adopted,
        style_correct=style_correct,
        gesture_adopted=gesture_adopted,
        gesture_correct=gesture_correct,
        command_correct=command_correct,
        prohibition_correct=prohibition_correct,
        command_hit=row["command_case"] and source == "command" and gesture_correct,
        natural_hit=row["natural_case"] and source == "dialogue" and gesture_correct,
        critical_misfire=((command != "none" and not command_correct) or prohibited_violation),
        raw_style=actual_decision.intent.style if reply_available else None,
        raw_gesture=actual_decision.intent.gesture if reply_available else None,
        style=cue.intent.style,
        gesture=gesture,
        gesture_source=source,
        command=command,
        prohibited=prohibited,
        style_confidence=actual_decision.style_confidence if reply_available else None,
        gesture_confidence=actual_decision.gesture_confidence if reply_available else None,
        perform_probability=actual_decision.perform_probability if reply_available else None,
        energy=cue.energy,
        command_confidence=user_decision.command_confidence,
        command_probability=user_decision.command_probability,
        prohibition_confidence=user_decision.prohibition_confidence,
        prohibition_probability=user_decision.prohibition_probability,
    )
    return row


def percentile(values: list[float], fraction: float) -> float | None:
    """线性插值保留小样本波动，无成功样本明确缺失而不是零延迟。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


def ratio(numerator: int, denominator: int) -> float | None:
    """零采纳不能得到满分，null会让真实验收门失败。"""
    return round(numerator / denominator, 4) if denominator else None


def summarize(rows: list[dict[str, Any]], latencies: list[float]) -> dict[str, Any]:
    """自然85%与指令95%分别验收，全部降级和禁令漏执行不能被总体平均掩盖。"""
    accepted = [row for row in rows if row["accepted"]]
    styles = [row for row in rows if row["style_adopted"]]
    gestures = [row for row in rows if row["gesture_adopted"]]
    commands = [row for row in rows if row["command_case"]]
    natural = [row for row in rows if row["natural_case"]]
    degraded = sum(row["degraded"] for row in rows)
    precision = ratio(sum(row["correct"] for row in accepted), len(accepted))
    gesture_precision = ratio(sum(row["gesture_correct"] for row in gestures), len(gestures))
    command_rate = ratio(sum(row["command_hit"] for row in commands), len(commands))
    natural_rate = ratio(sum(row["natural_hit"] for row in natural), len(natural))
    critical_misfires = sum(row["critical_misfire"] for row in rows)
    return {
        "total_cases": len(rows),
        "categories": dict(sorted(Counter(row["category"] for row in rows).items())),
        "accepted_decisions": len(accepted),
        "accepted_precision": precision,
        "decision_coverage": ratio(len(accepted), len(rows)),
        "style_precision": ratio(sum(row["style_correct"] for row in styles), len(styles)),
        "gesture_precision": gesture_precision,
        "command_cases": len(commands),
        "command_coverage": command_rate,
        "natural_gesture_cases": len(natural),
        "natural_gesture_coverage": natural_rate,
        "critical_misfires": critical_misfires,
        "degraded_cases": degraded,
        "successful_semantic_cases": len(latencies),
        "network_latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
        },
        "gate_passed": (
            len(rows) >= 257
            and degraded == 0
            and precision is not None
            and precision >= 0.95
            and gesture_precision is not None
            and gesture_precision >= 0.95
            and command_rate is not None
            and command_rate >= 0.95
            and natural_rate is not None
            and natural_rate >= 0.85
            and critical_misfires == 0
        ),
    }


def mock_response(case: dict[str, Any], *, user: bool = False) -> dict[str, Any]:
    """人工标签仅验证标准API契约，不冒称真实模型理解或代理连通性。"""
    style, gesture = case["allowed_styles"][0], case["allowed_gestures"][0]
    command, prohibited = case["allowed_commands"][0], case["allowed_prohibited"][0]
    if user and (command != "none" or case["phase"] != "listening"):
        gesture = "none"
    options = [("style", style, STYLES), ("gesture", gesture, GESTURES)]
    if user:
        options += [
            ("requested_gesture", command, GESTURES | {"repeat"}),
            ("prohibited_gesture", prohibited, GESTURES | {"all"}),
        ]
    answers = {
        name: {
            "type": "choice",
            "choice": selected,
            "probabilities": {option: float(option == selected) for option in labels},
            "confidence": 1.0,
        }
        for name, selected, labels in options
    }
    answers["energy"] = {
        "type": "score",
        "score": 1.0,
        "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0},
        "legend": {"0": "Quiet, comforting", "1": "Natural", "2": "Excited"},
        "confidence": 1.0,
    }
    return {
        "model": JEV_RESPONSE_MODEL,
        "answers": answers,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def evaluate_mock(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """固定响应经过正式双解析和仲裁，不读取凭据、不访问HTTP，不证明线上质量。"""
    rows = []
    for revision, case in enumerate(cases, 1):
        context = make_context(case, revision)
        decision = JevPresentationDecider._parse(mock_response(case))
        user_decision = JevPresentationDecider._parse_user(mock_response(case, user=True))
        rows.append(assess_decision(case, context, decision, user_decision))
    return {
        "mode": "mock-contract",
        "model": PINNED_MODEL,
        "online_acceptance": "未执行；Mock只证明契约和仲裁，不代表真实Jev中文准确率",
        "summary": summarize(rows, []),
        "cases": rows,
    }


def read_live_configuration(env_file: Path) -> tuple[str, str, str, str]:
    """显式live才读凭据，与生产共用代理校验，诊断不能静默绕过出口。"""
    load_dotenv(env_file, override=False)
    key = os.environ.get("VOICE_AGENT_JEV_API_KEY", "").strip()
    if not key:
        raise ValueError("请先在.env.local填写VOICE_AGENT_JEV_API_KEY")
    base_url = os.environ.get("VOICE_AGENT_JEV_BASE_URL", JEV_BASE_URL).strip()
    model = os.environ.get("VOICE_AGENT_JEV_MODEL", PINNED_MODEL).strip()
    proxy_url = os.environ.get("VOICE_AGENT_JEV_PROXY_URL", JEV_PROXY_URL).strip()
    validate_jev_configuration(base_url, model, key, proxy_url)
    return base_url, model, key, proxy_url


async def collect_decisions(
    decider: JevPresentationDecider,
    context: PresentationContext,
    budget: float,
) -> tuple[UserPresentationDecision | None, PresentationDecision | None]:
    """每分支保存已完成结果；另一请求超时不能抹掉先完成的独立指令许可。"""
    user_task = asyncio.create_task(decider.decide_user(make_user_context(context)))
    reply_task = (
        asyncio.create_task(decider.decide(context)) if context.phase == "speaking" else None
    )
    tasks = [user_task] + ([reply_task] if reply_task is not None else [])
    try:
        await asyncio.wait(tasks, timeout=budget)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # 所有取消均等待回收；成功值和异常分别处理，避免整体 gather 赋值丢失成功分支。
        results = await asyncio.gather(*tasks, return_exceptions=True)
    user_decision = results[0] if isinstance(results[0], UserPresentationDecision) else None
    if reply_task is None:
        decision = user_decision.presentation if user_decision is not None else None
    else:
        decision = results[1] if isinstance(results[1], PresentationDecision) else None
    return user_decision, decision


async def evaluate(
    cases: list[dict[str, Any]],
    base_url: str,
    model: str,
    key: str,
    proxy_url: str = JEV_PROXY_URL,
) -> dict[str, Any]:
    """外层按领域最长分支加回收余量等待，避免截断正式端口预算并保留独立结果。"""
    decider = JevPresentationDecider(
        base_url=base_url, model=model, api_key=key, proxy_url=proxy_url
    )
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    source_paths = (
        "src/xiaoya/domain/presentation.py",
        "src/xiaoya/domain/presentation_timing.py",
        "src/xiaoya/infrastructure/jev.py",
        "scripts/evaluate_jev.py",
        "tests/data/jev-chinese-cases.json",
    )
    source_fingerprints = {
        path: hashlib.sha256((PROJECT_ROOT / path).read_bytes()).hexdigest()
        for path in source_paths
    }
    try:
        for revision, case in enumerate(cases, 1):
            context = make_context(case, revision)
            started = time.perf_counter()
            budget = (
                PRESENTATION_TIMING.listening_total
                if context.phase == "listening"
                else PRESENTATION_TIMING.user_total
            ) + PRESENTATION_TIMING.application_grace
            user_decision, decision = await collect_decisions(decider, context, budget)
            elapsed = (time.perf_counter() - started) * 1000
            row = assess_decision(case, context, decision, user_decision)
            row["network_latency_ms"] = round(elapsed, 2)
            if not row["degraded"]:
                latencies.append(elapsed)
            rows.append(row)
            print(f"{revision}/{len(cases)} {case['id']}", file=sys.stderr)
    finally:
        await decider.aclose()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "mode": "live",
        "source_fingerprints": source_fingerprints,
        "sources_unchanged": all(
            hashlib.sha256((PROJECT_ROOT / path).read_bytes()).hexdigest() == fingerprint
            for path, fingerprint in source_fingerprints.items()
        ),
        "limits": {
            "style_confidence": 0.65,
            "gesture_confidence": 0.85,
            "perform_probability": 0.90,
            "accepted_precision": 0.95,
            "gesture_precision": 0.95,
            "command_coverage": 0.95,
            "natural_gesture_coverage": 0.85,
            "critical_misfires": 0,
        },
        "summary": summarize(rows, latencies),
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    """默认校验数据，显式live才消费额度；已有报告拒绝覆盖，错误不带敏感配置。"""
    parser = argparse.ArgumentParser(description="小芽自然表现中文验收；默认离线，--live才联网")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true", help="离线验证双输入Mock契约与仲裁")
    modes.add_argument(
        "--live", action="store_true", help="显式发送固定样例到官方Jev，消耗接口额度"
    )
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--output", type=Path, help="保存脱敏JSON报告；已有报告拒绝覆盖")
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.exists():
            raise ValueError("已有验收报告必须保留，请选择新的输出路径")
        cases = read_cases(args.cases)
        if args.live:
            base_url, model, key, proxy_url = read_live_configuration(args.env_file)
            report = asyncio.run(evaluate(cases, base_url, model, key, proxy_url))
        elif args.mock:
            report = evaluate_mock(cases)
        else:
            report = {
                "mode": "offline-validation",
                "total_cases": len(cases),
                "categories": dict(sorted(Counter(case["category"] for case in cases).items())),
                "online_acceptance": "未执行；请填写Key后显式运行--live",
            }
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0 if not (args.live or args.mock) or report["summary"]["gate_passed"] else 1
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Jev验收未完成：{type(error).__name__}；请检查配置或样例文件", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
