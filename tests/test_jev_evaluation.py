"""离线证明中文验收统计不会把降级、低覆盖或旧基线误报为通过。"""

import importlib.util
from pathlib import Path

import pytest

from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision

TOOL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_jev.py"
SPEC = importlib.util.spec_from_file_location("jev_evaluation_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


@pytest.fixture
def cases() -> list[dict]:
    """真实固定样例是覆盖门的依据，测试不自行制造更容易的精简集合。"""
    return TOOL.read_cases(TOOL.DEFAULT_CASES)


def test_fixed_chinese_cases_cover_all_phases_and_gestures(cases: list[dict]) -> None:
    """保证 100 条以上的固定中文样例和五手势正向覆盖持续存在。"""
    assert len(cases) == 138
    positives = [case for case in cases if case["category"] == "positive"]
    assert len(positives) == 30
    assert {case["allowed_gestures"][0] for case in positives} == {
        "wave",
        "nod",
        "tilt",
        "shy",
        "shake",
    }
    assert {case["phase"] for case in cases} == {"speaking", "listening", "thinking"}
    assert all(TOOL.make_context(case, 1).turn_id == case["id"] for case in cases)


def test_no_decisions_cannot_report_perfect_precision(cases: list[dict]) -> None:
    """全部降级必须留下无样本精度和零覆盖，禁止零除兜底为满分。"""
    rows = [TOOL.assess_decision(case, TOOL.make_context(case, 1), None) for case in cases]
    summary = TOOL.summarize(rows, [])
    assert summary["accepted_precision"] is None
    assert summary["decision_coverage"] == 0
    assert summary["positive_gesture_coverage"] == 0
    assert summary["degraded_cases"] == 138
    assert summary["network_latency_ms"] == {"p50": None, "p95": None}
    assert not summary["gate_passed"]


def test_rejecting_all_gestures_cannot_pass_coverage(cases: list[dict]) -> None:
    """保守地拒绝全部动作可能有正确表情，但仍必须因正向动作覆盖不足失败。"""
    rows = []
    for case in cases:
        context = TOOL.make_context(case, 1)
        decision = PresentationDecision(
            DeliveryIntent(case["allowed_styles"][0]), 0.99, 0.99, 0, 0.5
        )
        rows.append(TOOL.assess_decision(case, context, decision))
    summary = TOOL.summarize(rows, [100.0] * 138)
    assert summary["accepted_precision"] == 1.0
    assert summary["positive_gesture_coverage"] == 0
    assert not summary["gate_passed"]


def test_missing_positive_gesture_is_coverage_loss_not_precision_error(cases: list[dict]) -> None:
    """漏执行动作由覆盖率统计，不能把正确采纳的表情扭曲为错误动作。"""
    case = next(case for case in cases if case["category"] == "positive")
    context = TOOL.make_context(case, 1)
    decision = PresentationDecision(DeliveryIntent("happy"), 0.99, 0.99, 0, 0.5)
    row = TOOL.assess_decision(case, context, decision)
    assert row["accepted"] and row["correct"]
    assert not row["gesture_adopted"] and not row["positive_hit"]


@pytest.mark.parametrize("category", sorted(TOOL.CRITICAL_CATEGORIES))
def test_critical_misfire_fails_gate(cases: list[dict], category: str) -> None:
    """否定、引用、假设和目录外动作各自都属于零容忍的实际误执行。"""
    rows = []
    for case in cases:
        context = TOOL.make_context(case, 1)
        gesture = case["allowed_gestures"][0]
        if case["category"] == category:
            gesture = "wave"
        decision = PresentationDecision(
            DeliveryIntent(case["allowed_styles"][0], gesture), 0.99, 0.99, 0.99, 0.5
        )
        rows.append(TOOL.assess_decision(case, context, decision))
    summary = TOOL.summarize(rows, [100.0] * 138)
    assert summary["critical_misfires"] > 0
    assert not summary["gate_passed"]


def test_low_confidence_prevents_adoption(cases: list[dict]) -> None:
    """指标必须使用正式仲裁后的阈值，不能统计原始模型猜测为已执行。"""
    case = next(case for case in cases if case["category"] == "emotion")
    context = TOOL.make_context(case, 1)
    conflicting = PresentationDecision(DeliveryIntent("happy", "wave"), 0.64, 0.84, 0.99, 0.5)
    row = TOOL.assess_decision(case, context, conflicting)
    assert not row["style_adopted"] and not row["gesture_adopted"]
    assert row["style"] == "neutral"
    assert row["gesture"] == "none"


def test_listening_never_executes_speaking_gesture(cases: list[dict]) -> None:
    """高置信度模型结果也不能让倾听阶段绕过确定的播放许可。"""
    case = next(case for case in cases if case["category"] == "listening")
    context = TOOL.make_context(case, 1)
    decision = PresentationDecision(DeliveryIntent("gentle", "wave"), 0.99, 0.99, 0.99, 0.5)
    row = TOOL.assess_decision(case, context, decision)
    assert row["style_adopted"]
    assert not row["gesture_adopted"]


def test_default_cli_never_reads_credentials_or_calls_network(monkeypatch, capsys) -> None:
    """默认运行是纯样例校验，误执行验收工具不能消费接口额度或读取密钥。"""

    def forbidden(*args, **kwargs):
        """触碰线上分支立即失败，使默认离线契约可被测试观察。"""
        raise AssertionError("默认命令不应访问线上分支")

    monkeypatch.setattr(TOOL, "read_live_configuration", forbidden)
    monkeypatch.setattr(TOOL, "evaluate", forbidden)
    assert TOOL.main([]) == 0
    output = capsys.readouterr().out
    assert '"mode": "offline-validation"' in output
    assert '"total_cases": 138' in output


def test_fixed_gates_can_pass_with_correct_real_adoptions(cases: list[dict]) -> None:
    """正向和关键反例都正确时门必须可达，避免验收公式本身永远失败。"""
    rows = []
    for case in cases:
        context = TOOL.make_context(case, 1)
        decision = PresentationDecision(
            DeliveryIntent(case["allowed_styles"][0], case["allowed_gestures"][0]),
            0.99,
            0.99,
            0.99,
            0.5,
        )
        rows.append(TOOL.assess_decision(case, context, decision))
    summary = TOOL.summarize(rows, [100.0, 200.0, 300.0])
    assert summary["gate_passed"]
    assert summary["network_latency_ms"] == {"p50": 200.0, "p95": 290.0}


def test_missing_live_key_has_sanitized_exit(monkeypatch, tmp_path, capsys) -> None:
    """显式线上执行缺凭据要失败，错误输出不泄露其他进程配置。"""
    monkeypatch.setenv("VOICE_AGENT_JEV_API_KEY", "")
    assert TOOL.main(["--live", "--env-file", str(tmp_path / "missing.env")]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "ValueError" in output.err


def test_mock_contract_uses_all_cases_and_never_claims_online_quality(cases: list[dict]) -> None:
    """完整 Mock 门证明响应解析与阶段仲裁，必须明确区分真实模型效果。"""
    report = TOOL.evaluate_mock(cases)
    assert report["mode"] == "mock-contract"
    assert report["summary"]["total_cases"] == 138
    assert report["summary"]["gate_passed"]
    assert report["summary"]["successful_network_requests"] == 0
    assert report["summary"]["network_latency_ms"] == {"p50": None, "p95": None}
    assert "不代表真实 Jev 中文准确率" in report["online_acceptance"]
