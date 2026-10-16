"""验收统计把自然、指令及禁令独立计分，不让Mock冒称真实模型通过。"""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision

TOOL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_jev.py"
SPEC = importlib.util.spec_from_file_location("jev_evaluation_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


@pytest.fixture
def cases() -> list[dict]:
    """固定样例决定覆盖门，测试不得自行构造更容易的集合替代验收。"""
    return TOOL.read_cases(TOOL.DEFAULT_CASES)


def expected_user(case: dict) -> UserPresentationDecision:
    """离线人工许可只验证统计，本方法不代表Jev能识别该语义。"""
    return UserPresentationDecision(
        PresentationDecision(DeliveryIntent(case["allowed_styles"][0]), 0.99, 0.99, 0, 0.5),
        command=case["allowed_commands"][0],
        command_confidence=0.99,
        command_probability=0.99,
        prohibited=case["allowed_prohibited"][0],
        prohibition_confidence=0.99,
        prohibition_probability=0.99,
    )


def expected_reply(case: dict) -> PresentationDecision:
    """人工选允许集首项让统计公式可达，原始语义质量仍需真实网络报告。"""
    return PresentationDecision(
        DeliveryIntent(case["allowed_styles"][0], case["allowed_gestures"][0]),
        0.99,
        0.99,
        0.99,
        0.5,
    )


def test_fixed_cases_preserve_legacy_and_add_natural(cases: list[dict]) -> None:
    """原157身份持续存在，新增100分别覆盖75自然手势和25不适合主动作的情境。"""
    assert len(cases) == 257
    legacy = [case for case in cases if case["category"] != "natural"]
    assert len(legacy) == 157
    assert {f"positive-{number:03d}" for number in range(1, 31)} <= {c["id"] for c in legacy}
    assert {f"short-{gesture}-declined" for gesture in ("nod", "tilt", "wave", "shy", "shake")} <= {
        c["id"] for c in legacy
    }
    naturals = [case for case in cases if case["category"] == "natural"]
    assert len(naturals) == 100
    for gesture in ("wave", "nod", "tilt", "shy", "shake"):
        group = [case for case in naturals if case["allowed_gestures"] == [gesture]]
        assert len(group) == 15
        assert all(case["allowed_sources"] == ["dialogue"] for case in group)
        assert all(case["allowed_commands"] == ["none"] for case in group)
    assert sum(case["allowed_gestures"] == ["none"] for case in naturals) == 25
    assert sum("none" not in case["allowed_commands"] for case in cases) == 41
    assert {case["phase"] for case in cases} == {"speaking", "listening", "thinking"}
    assert all(TOOL.make_context(case, 1).turn_id == case["id"] for case in cases)


def test_no_decisions_cannot_report_perfect_precision(cases: list[dict]) -> None:
    """全部降级留下无样本精度和双覆盖零分，不能用零除兜底制造满分。"""
    rows = [TOOL.assess_decision(case, TOOL.make_context(case, 1), None) for case in cases]
    summary = TOOL.summarize(rows, [])
    assert summary["accepted_precision"] is None
    assert summary["decision_coverage"] == 0
    assert summary["command_coverage"] == summary["natural_gesture_coverage"] == 0
    assert summary["degraded_cases"] == len(cases)
    assert summary["network_latency_ms"] == {"p50": None, "p95": None}
    assert not summary["gate_passed"]


def test_commands_cannot_fill_missing_natural_coverage(cases: list[dict]) -> None:
    """明确指令全部成功仍不能冒充普通聊天已经会自然表现。"""
    rows = []
    for case in cases:
        decision = PresentationDecision(
            DeliveryIntent(case["allowed_styles"][0]), 0.99, 0.99, 0, 0.5
        )
        rows.append(
            TOOL.assess_decision(case, TOOL.make_context(case, 1), decision, expected_user(case))
        )
    summary = TOOL.summarize(rows, [100.0] * len(cases))
    assert summary["accepted_precision"] == 1
    assert summary["command_coverage"] == 1
    assert summary["natural_gesture_coverage"] == 0
    assert not summary["gate_passed"]


def test_natural_cannot_fill_missing_command_coverage(cases: list[dict]) -> None:
    """回复自然说到动作不能替代独立用户命令许可和指令执行来源。"""
    rows = []
    for case in cases:
        user = expected_user(case)
        user = UserPresentationDecision(
            user.presentation,
            prohibited=user.prohibited,
            prohibition_confidence=0.99,
            prohibition_probability=0.99,
        )
        rows.append(
            TOOL.assess_decision(case, TOOL.make_context(case, 1), expected_reply(case), user)
        )
    summary = TOOL.summarize(rows, [100.0] * len(cases))
    assert summary["natural_gesture_coverage"] == 1
    assert summary["command_coverage"] == 0
    assert not summary["gate_passed"]


def test_missing_command_is_coverage_loss_not_false_execution(cases: list[dict]) -> None:
    """用户请求漏采纳由覆盖率计分，不能把没有执行冒称关键误执行。"""
    case = next(case for case in cases if case["allowed_commands"] == ["wave"])
    reply = PresentationDecision(DeliveryIntent(case["allowed_styles"][0]), 0.99, 0.99, 0, 0.5)
    row = TOOL.assess_decision(
        case, TOOL.make_context(case, 1), reply, UserPresentationDecision(reply)
    )
    assert row["accepted"] and row["correct"]
    assert not row["command_hit"] and not row["critical_misfire"]


@pytest.mark.parametrize("category", sorted(TOOL.CRITICAL_CATEGORIES))
def test_critical_command_misfire_fails_gate(cases: list[dict], category: str) -> None:
    """引用、假设、禁止和目录外请求误识别成命令各自属于零容忍失败。"""
    rows = []
    for case in cases:
        user = expected_user(case)
        if case["category"] == category and case["allowed_commands"] == ["none"]:
            user = UserPresentationDecision(
                user.presentation, command="wave", command_confidence=0.99, command_probability=0.99
            )
        rows.append(
            TOOL.assess_decision(case, TOOL.make_context(case, 1), expected_reply(case), user)
        )
    summary = TOOL.summarize(rows, [100.0] * len(cases))
    assert summary["critical_misfires"] > 0
    assert not summary["gate_passed"]


def test_legal_correction_is_not_quoted_command_misfire(cases: list[dict]) -> None:
    """目录外动作被温和纠正时允许自然轻摇头，不能套用旧版全部none规则。"""
    case = next(case for case in cases if case["category"] == "unsupported")
    reply = PresentationDecision(
        DeliveryIntent(case["allowed_styles"][0], "shake"), 0.99, 0.99, 0.99, 0.5
    )
    row = TOOL.assess_decision(case, TOOL.make_context(case, 1), reply, expected_user(case))
    assert row["gesture_source"] == "dialogue"
    assert row["gesture_correct"] and not row["critical_misfire"]


def test_prohibited_natural_action_is_critical_failure(cases: list[dict]) -> None:
    """禁令识别失败导致回复自然动作越权时，同样必须进入关键误执行统计。"""
    case = next(case for case in cases if case["id"] == "short-shake-negative")
    reply = PresentationDecision(
        DeliveryIntent(case["allowed_styles"][0], "shake"), 0.99, 0.99, 0.99, 0.5
    )
    row = TOOL.assess_decision(
        case, TOOL.make_context(case, 1), reply, UserPresentationDecision(reply)
    )
    assert row["critical_misfire"]
    blocked = TOOL.assess_decision(case, TOOL.make_context(case, 1), reply, expected_user(case))
    assert blocked["gesture"] == "none" and not blocked["critical_misfire"]


def test_low_confidence_prevents_adoption(cases: list[dict]) -> None:
    """指标使用正式阈值，原始猜测不能被统计成已授权动作。"""
    case = next(case for case in cases if case["category"] == "emotion")
    reply = PresentationDecision(DeliveryIntent("happy", "wave"), 0.64, 0.84, 0.99, 0.5)
    row = TOOL.assess_decision(case, TOOL.make_context(case, 1), reply, expected_user(case))
    assert not row["style_adopted"] and not row["gesture_adopted"]
    assert row["style"] == "neutral" and row["gesture"] == "none"


def test_reply_timeout_retains_independent_command(cases: list[dict]) -> None:
    """回复增强丢失不能把先完成的用户命令统计为漏判，仍明确保留网络降级。"""
    case = next(case for case in cases if case["id"] == "positive-001")
    row = TOOL.assess_decision(case, TOOL.make_context(case, 1), None, expected_user(case))
    assert row["command_hit"] and row["gesture_source"] == "command"
    assert row["degraded"] and row["reply_degraded"] and not row["user_degraded"]
    assert not row["style_adopted"] and row["raw_style"] is None


@pytest.mark.parametrize("slow_user", [False, True])
def test_branch_timeout_keeps_completed_result(cases: list[dict], slow_user: bool) -> None:
    """以真实 asyncio 取消竞争验证分支结果保留，且超时任务必须完成回收。"""
    case = cases[0]
    context = TOOL.make_context(case, 1)
    canceled = []

    class FakeDecider:
        """仅替换网络延迟，保留正式领域值与真实任务取消路径。"""

        async def decide_user(self, user_context):
            """用户快分支的许可必须在回复慢分支取消后仍存在。"""
            try:
                await asyncio.sleep(1 if slow_user else 0)
                return expected_user(case)
            finally:
                canceled.append("user")

        async def decide(self, reply_context):
            """反向竞争也保留回复结果，不能把所有超时都归咎于用户识别。"""
            try:
                await asyncio.sleep(0 if slow_user else 1)
                return expected_reply(case)
            finally:
                canceled.append("reply")

    user, reply = asyncio.run(TOOL.collect_decisions(FakeDecider(), context, 0.02))
    assert (user is None) is slow_user
    assert (reply is None) is not slow_user
    assert sorted(canceled) == ["reply", "user"]


@pytest.mark.parametrize("phase,budget", [("listening", 1.4), ("thinking", 8.2), ("speaking", 8.2)])
def test_evaluate_uses_domain_outer_budget_and_fingerprints_timing(
    cases: list[dict], monkeypatch, phase: str, budget: float
) -> None:
    """脚本外层必须容纳正式最长分支，时序源码也需进入报告指纹以区分旧预算证据。"""
    case = next(case for case in cases if case["phase"] == phase)
    received_budgets = []
    closed = []

    class FakeDecider:
        """仅替换客户端生命周期，阻止预算回归访问真实模型。"""

        async def aclose(self):
            """不论分类阶段都应关闭一次客户端，避免扩大等待预算后遗留资源。"""
            closed.append(True)

    def make_decider(**kwargs):
        """配置仍从正式入口传入，测试只截断网络客户端创建。"""
        return FakeDecider()

    async def collect(decider, context, outer_budget):
        """观察正式评估传入的总预算，不用真实睡眠等待八秒。"""
        received_budgets.append(outer_budget)
        user = expected_user(case)
        return user, user.presentation if phase != "speaking" else expected_reply(case)

    monkeypatch.setattr(TOOL, "JevPresentationDecider", make_decider)
    monkeypatch.setattr(TOOL, "collect_decisions", collect)
    report = asyncio.run(TOOL.evaluate([case], "unused", "unused", "unused"))
    assert received_budgets == [pytest.approx(budget)]
    assert closed == [True] and report["sources_unchanged"]
    timing_path = "src/xiaoya/domain/presentation_timing.py"
    assert (
        report["source_fingerprints"][timing_path]
        == TOOL.hashlib.sha256((TOOL.PROJECT_ROOT / timing_path).read_bytes()).hexdigest()
    )


def test_evaluate_real_collector_retains_command_when_reply_times_out(
    cases: list[dict], monkeypatch
) -> None:
    """正式评估复用真实任务收集器，慢回复取消后有效指令和失败证据必须同时保留。"""
    case = next(case for case in cases if case["id"] == "positive-001")
    assert case["phase"] == "speaking"
    received_budgets = []
    released = []
    original_wait = asyncio.wait

    class FakeDecider:
        """用可取消慢任务模拟请求失败，不发送网络也不替换收集和统计逻辑。"""

        async def decide_user(self, context):
            """先完成的正式领域许可必须跨回复取消继续计入命令覆盖。"""
            return expected_user(case)

        async def decide(self, context):
            """慢回复只控制时序，取消后的回收记录用于防止悬挂任务。"""
            try:
                await asyncio.sleep(1)
                return expected_reply(case)
            finally:
                released.append("reply")

        async def aclose(self):
            """统计完成后客户端仍需关闭，不能因保留部分成功而跳过回收。"""
            released.append("client")

    def make_decider(**kwargs):
        """保留正式 evaluate 路径，只替换外部客户端。"""
        return FakeDecider()

    async def short_wait(tasks, *, timeout):
        """记录正式八秒预算后缩短本地时钟，不更改任务完成和取消语义。"""
        received_budgets.append(timeout)
        return await original_wait(tasks, timeout=0.02)

    monkeypatch.setattr(TOOL, "JevPresentationDecider", make_decider)
    monkeypatch.setattr(TOOL.asyncio, "wait", short_wait)
    report = asyncio.run(TOOL.evaluate([case], "unused", "unused", "unused"))
    row = report["cases"][0]
    assert received_budgets == [pytest.approx(8.2)]
    assert row["command_hit"] and row["gesture_source"] == "command"
    assert row["degraded"] and row["reply_degraded"] and not row["user_degraded"]
    assert report["summary"]["network_latency_ms"] == {"p50": None, "p95": None}
    assert released == ["reply", "client"]


def test_listening_only_permits_small_head_response(cases: list[dict]) -> None:
    """倾听可有轻反应，但全幅挥手不能绕过正式阶段和动作类型约束。"""
    case = next(case for case in cases if case["category"] == "listening")
    reply = PresentationDecision(DeliveryIntent("gentle", "wave"), 0.99, 0.99, 0.99, 0.5)
    row = TOOL.assess_decision(case, TOOL.make_context(case, 1), reply, expected_user(case))
    assert row["style_adopted"] and not row["gesture_adopted"]


def test_default_cli_never_reads_credentials_or_calls_network(monkeypatch, capsys) -> None:
    """默认执行只校验固定数据，误运行不能读取Key或消费接口额度。"""

    def forbidden(*args, **kwargs):
        """触碰线上分支立即失败，让默认离线契约可被观察。"""
        raise AssertionError("默认命令不应访问线上分支")

    monkeypatch.setattr(TOOL, "read_live_configuration", forbidden)
    monkeypatch.setattr(TOOL, "evaluate", forbidden)
    assert TOOL.main([]) == 0
    output = capsys.readouterr().out
    assert '"mode": "offline-validation"' in output and '"total_cases": 257' in output


def test_fixed_gates_can_pass_with_correct_adoptions(cases: list[dict]) -> None:
    """全部语义标签正确时验收门可达，统计公式不能永远失败。"""
    rows = [
        TOOL.assess_decision(
            case, TOOL.make_context(case, 1), expected_reply(case), expected_user(case)
        )
        for case in cases
    ]
    summary = TOOL.summarize(rows, [100.0, 200.0, 300.0])
    assert summary["gate_passed"]
    assert summary["network_latency_ms"] == {"p50": 200.0, "p95": 290.0}


def test_positive_subset_cannot_claim_full_gate(cases: list[dict]) -> None:
    """子集用于调试却不能代替全257门，即使正向覆盖与精度都满分也必须失败。"""
    rows = TOOL.evaluate_mock(cases)["cases"]
    subset = [row for row in rows if row["command_case"] or row["category"] == "natural"]
    assert 100 < len(subset) < 257
    summary = TOOL.summarize(subset, [100.0] * len(subset))
    assert summary["command_coverage"] == summary["natural_gesture_coverage"] == 1
    assert summary["gesture_precision"] == 1 and not summary["gate_passed"]


def test_missing_live_key_has_sanitized_exit(monkeypatch, tmp_path, capsys) -> None:
    """显式线上执行缺凭据要失败，错误输出不泄露其他进程配置。"""
    monkeypatch.setenv("VOICE_AGENT_JEV_API_KEY", "")
    assert TOOL.main(["--live", "--env-file", str(tmp_path / "missing.env")]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "ValueError" in output.err


def test_live_requires_same_proxy_as_runtime(monkeypatch, tmp_path) -> None:
    """代理配置损坏不能偷偷直连，诊断不得借用其他供应商密钥。"""
    monkeypatch.setenv("VOICE_AGENT_JEV_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("VOICE_AGENT_JEV_BASE_URL", "https://openrouter.ai")
    monkeypatch.setenv("VOICE_AGENT_JEV_MODEL", "typesafe/jev-1.13")
    monkeypatch.setenv("VOICE_AGENT_JEV_PROXY_URL", "http://127.0.0.1:10808")
    assert TOOL.read_live_configuration(tmp_path / "missing.env") == (
        "https://openrouter.ai",
        "typesafe/jev-1.13",
        "test-openrouter-key",
        "http://127.0.0.1:10808",
    )
    monkeypatch.setenv("VOICE_AGENT_JEV_PROXY_URL", "")
    with pytest.raises(ValueError, match="VOICE_AGENT_JEV_PROXY_URL"):
        TOOL.read_live_configuration(tmp_path / "missing.env")


def test_mock_contract_is_not_online_quality(cases: list[dict]) -> None:
    """完整Mock只证明双解析和仲裁，网络精度与延迟必须保持未执行。"""
    report = TOOL.evaluate_mock(cases)
    assert report["mode"] == "mock-contract"
    assert report["summary"]["total_cases"] == 257 and report["summary"]["gate_passed"]
    assert report["summary"]["successful_semantic_cases"] == 0
    assert report["summary"]["network_latency_ms"] == {"p50": None, "p95": None}
    assert "不代表真实Jev中文准确率" in report["online_acceptance"]


def test_existing_report_is_rejected_before_live_work(monkeypatch, tmp_path, capsys) -> None:
    """报告拒绝覆盖必须发生在读取凭据或网络请求之前，失败证据才不会被改写。"""
    output = tmp_path / "receipt.json"
    output.write_text("original failure", encoding="utf-8")

    def forbidden(*args):
        """已存在输出时触碰凭据便说明保护顺序错误。"""
        raise AssertionError("不得进入线上配置")

    monkeypatch.setattr(TOOL, "read_live_configuration", forbidden)
    assert TOOL.main(["--live", "--output", str(output)]) == 2
    assert output.read_text(encoding="utf-8") == "original failure"
    assert "ValueError" in capsys.readouterr().err


def test_source_label_cannot_turn_natural_samples_into_commands(
    cases: list[dict], tmp_path
) -> None:
    """来源标签被误改时直接拒绝数据，避免指令偷填自然覆盖。"""
    case = next(case for case in cases if case["id"] == "natural-wave-001")
    case["allowed_sources"] = ["command"]
    output = tmp_path / "cases.json"
    output.write_text(
        json.dumps({"version": 2, "language": "zh-CN", "cases": cases}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="自然动作不能预置指令"):
        TOOL.read_cases(output)
