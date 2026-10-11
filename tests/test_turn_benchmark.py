"""只验证诊断边界与公开 I/O，不加载真实声学权重或连接模型服务。"""

import asyncio
import importlib.util
import json
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import AgentSession

TOOL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_turns.py"
SPEC = importlib.util.spec_from_file_location("turn_benchmark_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


def test_empty_statistics_and_missing_question_coverage_never_choose_parameters():
    """未执行和素材缺失必须维持未验收，不得把空样本默认成最快组合。"""
    assert TOOL.distribution([]) == {"count": 0, "p50": None, "p95": None}
    summary = TOOL.summarize([], ["缺少不完整问题"], private_stt=False)
    assert summary["acoustic_candidate"] is None
    assert not summary["production_optimum_selected"]
    assert not summary["private_recognition_verified"]
    assert all(not group["quality_passed"] for group in summary["groups"])


def test_declared_percentiles_and_fixed_config_do_not_use_cloud_defaults():
    """统计口径一致，350/500ms 属于 SDK interruption 而非 Silero 起音门槛。"""
    assert TOOL.percentile([40, 10, 30, 20], 95) == 38.5
    detector = object()
    for delay, duration in TOOL.GRID:
        options = TOOL.public_turn_options(delay, duration, detector)
        assert options["turn_detection"] is detector
        assert options["endpointing"] == {"mode": "fixed", "min_delay": delay, "max_delay": 1.10}
        assert options["interruption"]["mode"] == "vad"
        assert options["interruption"]["min_duration"] == duration
        assert options["interruption"]["resume_false_interruption"]
    with pytest.raises(ValueError):
        TOOL.public_turn_options(0.1, 0.5, detector)


def assessed_row(kind: str, events: list[dict]) -> dict:
    """样本标注明确区分完整句对照与不完整问题，不由模型结果反推预期标签。"""
    return TOOL.assess_trial(
        {
            "kind": kind,
            "events": events,
            "input_started_mono": 10,
            "last_speech_seconds": 2,
            "error_type": None,
            "resources_closed": True,
            "actual_eot_predictions": 1,
        }
    )


def test_complete_sentence_pause_does_not_inherit_incomplete_question_failure_label():
    """两句完整话之间正常接话不能被统计器改称抢话；同一时序用于不完整问题则必须失败。"""
    events = [
        {"kind": "turn_completed", "mono": 11, "text": "你好。"},
        {"kind": "audio_start", "mono": 11.1},
    ]
    control = assessed_row("complete-pause-control", events)
    incomplete = assessed_row("incomplete-pause", events)
    assert control["premature_turns"] == control["premature_audio"] == 0
    assert incomplete["premature_turns"] == incomplete["premature_audio"] == 1


def test_no_audio_cannot_pass_quality_even_if_one_turn_was_committed():
    """SDK 已确认轮次却没有播放帧时，不能从剩余更快样本中选择合格参数。"""
    rows = []
    for kind in ("latency", "incomplete-pause"):
        row = assessed_row(kind, [{"kind": "turn_completed", "mono": 13, "text": "好的。"}])
        row.update({"min_delay": 0.6, "min_duration": 0.35})
        rows.append(row)
    summary = TOOL.summarize(rows, [], private_stt=True)
    assert summary["acoustic_candidate"] is None
    assert summary["groups"][0]["quality_failures"] == 1
    assert not summary["browser_first_sound_verified"]


def test_plan_and_explicit_mode_boundary_never_initialize_models(monkeypatch, capsys):
    """计划检查和默认调用只列计划，不能悄悄加载权重或干扰同时进行的私有验收。"""
    initialize = Mock(side_effect=AssertionError("不能加载模型"))
    monkeypatch.setattr(TOOL, "init_eot", initialize)
    monkeypatch.setattr(TOOL.silero.VAD, "load", initialize)
    monkeypatch.setattr(TOOL, "read_cases", Mock(return_value=([], ["缺少不完整问题"])))
    assert TOOL.main(["--plan"]) == 0
    assert json.loads(capsys.readouterr().out)["blocked_categories"] == ["缺少不完整问题"]
    assert TOOL.main([]) == 0
    assert json.loads(capsys.readouterr().out)["blocked_categories"] == ["缺少不完整问题"]
    initialize.assert_not_called()


def test_partial_coverage_and_noise_only_private_rows_cannot_pass_acceptance():
    """单个完美样本不能替代六组合完整覆盖，只有噪声记录也不能宣称识别已验证。"""
    rows = []
    for kind in ("latency", "incomplete-pause"):
        row = assessed_row(
            kind,
            [
                {"kind": "turn_completed", "mono": 13, "text": "完整问题"},
                {"kind": "audio_start", "mono": 13.1},
            ],
        )
        row.update({"min_delay": 0.6, "min_duration": 0.35})
        rows.append(row)
    summary = TOOL.summarize(rows, [], private_stt=True)
    assert summary["groups"][0]["quality_failures"] == 0
    assert not summary["groups"][0]["coverage_complete"]
    assert summary["acoustic_candidate"] is None
    assert not summary["private_recognition_verified"]
    noise = assessed_row("noise-only", [])
    noise.update({"min_delay": 0.6, "min_duration": 0.35})
    assert not TOOL.summarize([noise], [], private_stt=True)["private_recognition_verified"]


def test_acoustic_trim_keeps_padding_and_declares_no_human_review(monkeypatch):
    """合成素材只按信号边界裁切，明确无人收听，静音和过量削波必须拒绝。"""
    monkeypatch.syspath_prepend(str(TOOL_PATH.parent))
    preparation = importlib.import_module("prepare_turn_materials")
    active = np.full(16000, 2000, dtype="<i2")
    pcm = np.concatenate((np.zeros(8000, dtype="<i2"), active, np.zeros(8000, dtype="<i2")))
    cropped, review = preparation.trim_edges(pcm, 16000)
    assert len(cropped) == 16640
    assert review["crop_from_frame"] == 7680
    assert review["crop_to_frame"] == 24320
    assert review["human_listened"] is False
    with pytest.raises(ValueError):
        preparation.trim_edges(np.zeros(16000, dtype="<i2"), 16000)
    with pytest.raises(ValueError):
        preparation.trim_edges(np.full(16000, 32767, dtype="<i2"), 16000)


def test_tts_block_edges_do_not_masquerade_as_acoustic_onset_or_end():
    """旧素材段落含一秒边缘静音，声学计时必须定位信号，重放事件仍如实标为人工调度。"""
    pcm = np.concatenate(
        (
            np.zeros(16000, dtype="<i2"),
            np.full(16000, 2000, dtype="<i2"),
            np.zeros(16000, dtype="<i2"),
        )
    )
    case = TOOL.make_case(
        "silence-edges", "latency", pcm, [{"text": "你好。", "from_seconds": 0, "to_seconds": 3}]
    )
    assert case["first_acoustic_seconds"] == 1
    assert case["last_speech_seconds"] == 2
    assert case["labelled_text_end_seconds"] == 3
    assert case["schedule"][-1]["at"] == pytest.approx(3.32)
    assert TOOL.acoustic_bounds(np.zeros(16000, dtype="<i2")) is None


def test_nonempty_wrong_recognition_is_not_declared_exact_or_semantically_repaired():
    """可连接且有文本不等于识别正确，文字规范化不能用猜测替换错误语义。"""
    row = TOOL.assess_trial(
        {
            "kind": "latency",
            "events": [{"kind": "turn_completed", "mono": 13, "text": "谢谢。"}],
            "input_started_mono": 10,
            "last_speech_seconds": 2,
            "expected_text": "中国的首都在哪里？",
        }
    )
    assert row["recognized_nonempty"] and not row["recognition_exact_match"]
    assert row["recognition_cer"] > 0
    assert TOOL.normalized_text(" 中国，首都？ ") == "中国首都"


async def wait_for_event(events: list[dict], kind: str) -> None:
    """事件驱动观察公开输出，只用有限超时保护测试挂起。"""
    async with asyncio.timeout(1):
        while not any(event["kind"] == kind for event in events):
            await asyncio.sleep(0.001)


def frame(seconds: float = 0.1):
    """固定非零 SDK 音频帧让暂停测试不依赖合成模型、声卡或 PCM 静音阈值。"""
    pcm = np.full(round(seconds * TOOL.RATE), 1000, dtype="<i2")
    return rtc.AudioFrame(
        data=pcm.tobytes(), sample_rate=TOOL.RATE, num_channels=1, samples_per_channel=len(pcm)
    )


async def test_pause_preserves_buffer_and_playhead_until_resumed():
    """暂停不是取消，冻结时不得前进或报告完成，恢复必须使用原有段身份。"""
    events = []
    output = TOOL.RealtimePlayback(events)
    try:
        await output.capture_frame(frame())
        output.flush()
        await wait_for_event(events, "audio_start")
        output.pause()
        await asyncio.sleep(0.01)
        position = output.segments[1]["played"]
        await asyncio.sleep(0.05)
        assert output.segments[1]["played"] == position
        assert not output.segments[1]["finished"]
        output.resume()
        finished = await asyncio.wait_for(output.wait_for_playout(), 1)
        assert not finished.interrupted
        assert finished.playback_position == pytest.approx(0.1, abs=0.001)
        assert sum(event["kind"] == "audio_start" for event in events) == 1
        assert [event["kind"] for event in events if event["kind"] in {"pause", "resume"}] == [
            "pause",
            "resume",
        ]
    finally:
        await output.aclose()
    assert output.worker.done()


async def test_clear_while_paused_discards_old_buffer_and_allows_new_segment():
    """SDK 取消后旧帧不得恢复，下一段仍拥有新的播放计数及完整播放时间。"""
    events = []
    output = TOOL.RealtimePlayback(events)
    try:
        await output.capture_frame(frame())
        await output.capture_frame(frame())
        output.flush()
        await wait_for_event(events, "audio_start")
        output.pause()
        output.clear_buffer()
        cancelled = await asyncio.wait_for(output.wait_for_playout(), 1)
        assert cancelled.interrupted
        await output.capture_frame(frame(0.02))
        output.flush()
        finished = await asyncio.wait_for(output.wait_for_playout(), 1)
        assert not finished.interrupted and finished.playback_position == pytest.approx(0.02)
        assert [event["segment"] for event in events if event["kind"] == "audio_start"] == [1, 2]
    finally:
        await output.aclose()


async def test_cancelled_input_backpressure_can_be_closed_and_readers_wake():
    """输入没人消费时取消并关闭仍完成，不能遗留满队列或空队列等待者。"""
    source = TOOL.RealtimeInput()
    task = asyncio.create_task(source.feed(np.zeros(16000, dtype="<i2")))
    await asyncio.sleep(0.10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await source.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(source.__anext__(), 0.5)
    assert source.closed


async def test_official_session_accepts_public_memory_io_without_models_or_room():
    """用官方 SDK 真启动手动模式验收公开 I/O，声学模型仍不初始化或推理。"""
    events = []
    source, output = TOOL.RealtimeInput(), TOOL.RealtimePlayback(events)
    recognizer, language_model, voice = TOOL.ReplaySTT(), TOOL.FixedLLM(), TOOL.FixedTTS()
    session = AgentSession(
        stt=recognizer,
        llm=language_model,
        tts=voice,
        turn_handling={"turn_detection": "manual"},
        user_away_timeout=None,
    )
    session.input.audio, session.output.audio = source, output
    agent = TOOL.MeasuredAgent({"schedule": []}, source, events, replay=True)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(output.aclose)
        cleanup.push_async_callback(source.aclose)
        cleanup.push_async_callback(voice.aclose)
        cleanup.push_async_callback(language_model.aclose)
        cleanup.push_async_callback(recognizer.aclose)
        cleanup.push_async_callback(session.aclose)
        await asyncio.wait_for(session.start(agent, record=False), 2)
        speech = session.say(
            "固定公开 I/O 验收", audio=TOOL.fixed_audio(0.06), add_to_chat_ctx=False
        )
        await asyncio.wait_for(speech.wait_for_playout(), 2)
        assert any(event["kind"] == "audio_start" for event in events)
    assert source.closed and output.closed and output.worker.done()


async def test_matrix_cancellation_keeps_closed_rows_and_marks_incomplete_progress(
    monkeypatch, tmp_path
):
    """矩阵取消必须保留已完成样本，正在运行的样本收尾后仍向调用者传播取消。"""
    entered, closed = asyncio.Event(), asyncio.Event()
    calls = 0

    async def trial(case, delay, duration, model, *, config):
        """隔离模型与时钟，仅让真实矩阵经历完成一次、第二次取消的边界。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        row = assessed_row("latency", [{"kind": "turn_completed", "mono": 13, "text": "你好"}])
        row.update({"id": case["id"], "min_delay": delay, "min_duration": duration})
        return row

    monkeypatch.setattr(TOOL, "init_eot", lambda: None)
    monkeypatch.setattr(TOOL.silero.VAD, "load", lambda **kwargs: object())
    monkeypatch.setattr(TOOL, "run_trial", trial)
    matrix = asyncio.create_task(
        TOOL.run_matrix(
            [{"id": "fixture", "kind": "latency"}],
            [],
            repeats=1,
            warmups=0,
            config=None,
            output=tmp_path,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    matrix.cancel()
    with pytest.raises(asyncio.CancelledError):
        await matrix
    assert closed.is_set()
    state = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert state["status"] == "interrupted"
    assert state["error_type"] == "CancelledError"
    assert state["formal_completed"] == 1 and state["formal_expected"] == 6
    rows = (tmp_path / "rows.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["resources_closed"]
    assert not (tmp_path / "summary.json").exists()


async def test_matrix_refuses_to_overwrite_previous_failure_before_model_init(
    tmp_path, monkeypatch
):
    """重复输出路径不能抹去此前失败或先加载模型，原始证据是诊断的必要交付。"""
    previous = '{"quality_passed":false}'
    (tmp_path / "summary.json").write_text(previous, encoding="utf-8")

    def unexpected_model():
        """被拒绝的运行没有推理权限，不能为了检查输出先启动真实模型。"""
        raise AssertionError("输出冲突后不应初始化模型")

    monkeypatch.setattr(TOOL, "init_eot", unexpected_model)
    with pytest.raises(FileExistsError):
        await TOOL.run_matrix([], [], repeats=1, warmups=0, config=None, output=tmp_path)
    assert (tmp_path / "summary.json").read_text(encoding="utf-8") == previous
    assert not (tmp_path / "run-plan.json").exists()
