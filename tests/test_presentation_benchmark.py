"""验证诊断统计和真实异步边界，默认测试不能调用私有模型或创建房间。"""

import asyncio
import importlib.util
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

TOOL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_presentation.py"
SPEC = importlib.util.spec_from_file_location("presentation_benchmark_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


def test_empty_latency_has_null_percentiles_and_cannot_pass_gate():
    """无有效音频必须呈现空值和未通过，不能通过零延迟的默认值掩盖失败。"""
    assert TOOL.latency_summary([]) == {"count": 0, "p50": None, "p95": None}
    report = TOOL.summarize([], Counter(), 1)
    assert report["valid_pairs"] == 0
    assert report["group_p95_increase_ms"] is None
    assert not report["gate_passed"]


@pytest.mark.parametrize(
    ("values", "percentage", "expected"),
    [([40.0, 10.0, 30.0, 20.0], 50, 25), ([10.0, 20.0, 30.0, 40.0], 95, 38.5), ([7.0], 95, 7)],
)
def test_small_sample_percentiles_use_declared_linear_interpolation(values, percentage, expected):
    """小样本分位数需符合报告公布的方法，避免两种模式采用不同的统计口径。"""
    assert TOOL.percentile(values, percentage) == expected


@pytest.mark.parametrize(("enabled", "passed"), [([300.0, 100.0], True), ([300.0, 500.0], False)])
def test_mode_p95_difference_and_paired_jitter_have_separate_meanings(enabled, passed):
    """相同模式分布的顺序波动不会改写 p95 验收，整体变慢仍必须失败。"""
    rows = []
    for pair, baseline in enumerate([100.0, 300.0]):
        for mode, value in (("without-jev", baseline), ("mock-jev", enabled[pair])):
            rows.append(
                {
                    "warmup": False,
                    "audio_complete": True,
                    "error_type": None,
                    "pair": pair,
                    "mode": mode,
                    "first_audio_frame_ms": value,
                    "worker_closed": True,
                }
            )
    report = TOOL.summarize(rows, Counter(), 2)
    assert report["gate_passed"] is passed
    assert report["paired_jitter_within_100ms"] is False


async def test_voice_first_frame_precedes_slow_jev_and_full_stream_is_closed():
    """观察模型尚未结束时首帧已到达，而不是只用耗时阈值推测并行正确性。"""
    counts = Counter()
    async with AsyncExitStack() as cleanup:
        voices = TOOL.make_voices(cleanup, counts, None)
        row = await TOOL.measure_segment(voices, counts, enabled=True, pair=0)
    assert row["audio_complete"] and row["frames"] == 3
    assert row["audio_duration_ms"] == pytest.approx(30)
    assert row["jev_started_at_first_frame"] and not row["jev_finished_at_first_frame"]
    assert row["worker_closed"] and row["error_type"] is None
    assert counts["jev_requests_cancelled"] == 1 and counts["jev_requests_returned"] == 0
    assert counts["audio_frames_consumed"] == 3
    assert counts["tts_streams_created"] == counts["tts_streams_closed"] == 1
    assert counts["tts_clients_created"] == counts["tts_clients_closed"] == 3
    assert TOOL.resources_balanced(counts)


async def test_cancelling_diagnostic_closes_stream_controller_and_clients(monkeypatch):
    """调用者取消仍释放已经打开的流及判断 worker，不能依赖脚本退出清除资源。"""
    entered = asyncio.Event()

    class StalledStream(TOOL.MockAudioStream):
        """在公开音频读取处停住，准确选择取消边界而不访问 SDK 私有状态。"""

        async def __aiter__(self):
            """阻塞发生在开流成功之后，测试必须证明真实上下文退出而非未分配资源。"""
            entered.set()
            await asyncio.Event().wait()
            if False:
                yield None

    monkeypatch.setattr(TOOL, "MockAudioStream", StalledStream)
    counts = Counter()
    async with AsyncExitStack() as cleanup:
        voices = TOOL.make_voices(cleanup, counts, None)
        task = asyncio.create_task(TOOL.measure_segment(voices, counts, enabled=True, pair=0))
        await asyncio.wait_for(entered.wait(), 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert counts["tts_streams_created"] == counts["tts_streams_closed"] == 1
    assert counts["jev_ports_created"] == counts["jev_ports_closed"] == 1
    assert TOOL.resources_balanced(counts)
