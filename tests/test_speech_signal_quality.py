"""首包只排除已知弱噪与稀疏脉冲，不把能量校验冒充语音可懂度或真实设备验收。"""

from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_speech_delivery import speech_delivery as speech_delivery

_WINDOW = 480


def _tone(windows: int = 3, amplitude: float = 0.009) -> bytes:
    """低于播放器 0.012 门的连续轻声也可通过服务端保守门，避免误称二者等价。"""
    phase = np.arange(windows * _WINDOW) * (2 * np.pi * 200 / 24000)
    return (np.sin(phase) * amplitude * 32767).astype("<i2").tobytes()


def _chunks(parts: list[bytes], closed: list[bool] | None = None):
    """使用可关闭的惰性源记录释放，不能以普通列表掩盖推理生成器的取消边界。"""
    try:
        yield from parts
    finally:
        if closed is not None:
            closed.append(True)


def _bad_signal(kind: str) -> bytes:
    """已知坏信号保留原量级，包含连续弱声、单脉冲、跨窗脉冲与逐窗稀疏尖峰。"""
    pcm = np.zeros(5 * _WINDOW, dtype="<i2")
    if kind == "quantization":
        pcm[:] = 4
    elif kind == "weak_noise":
        pcm[:] = np.resize(np.array([35, -35], dtype="<i2"), len(pcm))
    elif kind == "single_pulse":
        pcm[_WINDOW] = 32767
    elif kind == "cross_window_pulse":
        pcm[_WINDOW - 1 : _WINDOW + 1] = 32767
    elif kind.startswith("sparse_"):
        count = int(kind.removeprefix("sparse_"))
        pcm.reshape(-1, _WINDOW)[:, :count] = 32767
    return pcm.tobytes()


@pytest.mark.parametrize(
    "kind",
    [
        "silence",
        "quantization",
        "weak_noise",
        "single_pulse",
        "cross_window_pulse",
        "sparse_1",
        "sparse_24",
        "sparse_47",
    ],
)
def test_known_degenerate_signals_never_emit_first_packet(speech_delivery, kind) -> None:
    """旧 peak>3 会放出的退化结果必须在出站前失败，窗口占比挡住连续窗内少量尖峰。"""
    closed = []
    stream = speech_delivery.implementation._audible_pcm(_chunks([_bad_signal(kind)], closed), None)
    with pytest.raises(speech_delivery.implementation.SilentSynthesisError):
        next(stream)
    assert closed == [True]


@pytest.mark.parametrize("width", [1, 7, 959, 960, 961, 10000])
def test_alignment_preserves_leading_pause_gentle_signal_and_chunks(speech_delivery, width):
    """分析跨任意块和半采样边界连续对齐；240ms 停顿不判死，放行时不改原始分包。"""
    original = bytes(12 * _WINDOW * 2) + _tone(5) + bytes(2 * _WINDOW * 2)
    parts = [original[index : index + width] for index in range(0, len(original), width)]
    closed = []
    actual = list(speech_delivery.implementation._audible_pcm(_chunks(parts, closed), None))
    assert actual == parts
    assert b"".join(actual) == original
    assert closed == [True]


@pytest.mark.parametrize("amplitude,accepted", [(131, False), (132, True)])
def test_rms_boundary_uses_pcm_scale_without_peak_shortcut(speech_delivery, amplitude, accepted):
    """PCM16 的 .004 两侧由真实量化值验证，避免边界因归一化或整数平方溢出偏移。"""
    signal = np.full(3 * _WINDOW, amplitude, dtype="<i2").tobytes()
    stream = speech_delivery.implementation._audible_pcm(_chunks([signal]), None)
    if accepted:
        assert list(stream) == [signal]
    else:
        with pytest.raises(speech_delivery.implementation.SilentSynthesisError):
            next(stream)


@pytest.mark.parametrize("active_count,accepted", [(47, False), (48, True)])
def test_occupancy_boundary_requires_distributed_signal(speech_delivery, active_count, accepted):
    """每窗至少 48 点的有限约束排除稀疏峰；更密的合成信号可通过，不冒充音质判断。"""
    pcm = np.zeros((3, _WINDOW), dtype="<i2")
    pcm[:, :active_count] = 600
    signal = pcm.tobytes()
    stream = speech_delivery.implementation._audible_pcm(_chunks([signal]), None)
    if accepted:
        assert list(stream) == [signal]
    else:
        with pytest.raises(speech_delivery.implementation.SilentSynthesisError):
            next(stream)


@pytest.mark.parametrize("tail_samples", [0, 1, 479])
def test_partial_eof_window_cannot_complete_sixty_ms_gate(speech_delivery, tail_samples):
    """不足完整 20ms 的末尾不能凑成第三窗，以免短响声借 EOF 逃过持续时间约束。"""
    signal = _tone(2) + np.full(tail_samples, 1000, dtype="<i2").tobytes()
    with pytest.raises(speech_delivery.implementation.SilentSynthesisError):
        next(speech_delivery.implementation._audible_pcm(_chunks([signal]), None))


def test_failed_window_resets_continuous_run(speech_delivery) -> None:
    """两段各 40ms 的声音不能拼作连续 60ms，避免零散脉冲累计得到错误放行。"""
    signal = _tone(2) + bytes(_WINDOW * 2) + _tone(2)
    with pytest.raises(speech_delivery.implementation.SilentSynthesisError):
        next(speech_delivery.implementation._audible_pcm(_chunks([signal]), None))


def test_gate_releases_prefix_without_consuming_future_chunks(speech_delivery) -> None:
    """达到持续窗立即流式放行，不为全句质量检查等待完整推理，也不改后续音频。"""
    consumed = []
    parts = [bytes(_WINDOW * 2), _tone(), b"\x01\x00"]

    def source():
        """第三块只能由后续读取触发，用于证明首包不等待句末。"""
        for index, part in enumerate(parts):
            consumed.append(index)
            yield part

    stream = speech_delivery.implementation._audible_pcm(source(), None)
    assert next(stream) == parts[0]
    assert consumed == [0, 1]
    assert list(stream) == parts[1:]


def test_cancel_during_buffer_flush_drops_remaining_prefix(speech_delivery) -> None:
    """放行后逐块取消仍有效，不能把尚未出站的静音或语音缓存泄露到新轮次。"""
    cancelled = Event()
    closed = []
    parts = [bytes(_WINDOW * 2), _tone(), _tone()]
    stream = speech_delivery.implementation._audible_pcm(_chunks(parts, closed), cancelled)
    assert next(stream) == parts[0]
    cancelled.set()
    assert list(stream) == []
    assert closed == [True]


def test_exact_buffer_limit_is_allowed_but_one_more_byte_rejects(speech_delivery):
    """上限包含未成窗尾部；超限前停止读取并回收源，不让故障流无限积压。"""
    consumed = []
    closed = []

    def source():
        """精确上限仍等待信号，下一字节触发边界，后续块不得被读取。"""
        try:
            for index, part in enumerate([bytes(2**20), b"\0", _tone()]):
                consumed.append(index)
                yield part
        finally:
            closed.append(True)

    with pytest.raises(speech_delivery.implementation.SilentSynthesisError):
        next(speech_delivery.implementation._audible_pcm(source(), None))
    assert consumed == [0, 1]
    assert closed == [True]


@pytest.mark.parametrize("cancel_at", ["buffering", "flush"])
def test_cancellation_does_not_consume_retry_budget(speech_delivery, cancel_at):
    """实际模型路径取消不属于坏声音；不重试、释放锁，供应商仅由已有收尾排空。"""
    models = speech_delivery.models
    cancelled = Event()
    closed = []

    def pcm_source():
        """在真实首包门两侧制造取消，保持合成调度与重试逻辑均为实际实现。"""
        try:
            yield bytes(_WINDOW * 2)
            if cancel_at == "buffering":
                cancelled.set()
            yield _tone()
        finally:
            closed.append(True)

    models._voice.inference_zero_shot.return_value = _chunks([])
    models._pcm_chunks = Mock(return_value=pcm_source())
    stream = models.synthesize_stream("受控取消样本", cancelled=cancelled)
    if cancel_at == "flush":
        assert next(stream) == bytes(_WINDOW * 2)
        cancelled.set()
    assert list(stream) == []
    assert models._voice.inference_zero_shot.call_count == 1
    assert closed == [True]
    assert not models._tts_lock.locked()


@pytest.mark.parametrize("style", ["NEUTRAL", "HAPPY", "GENTLE"])
@pytest.mark.parametrize("kind", ["weak_noise", "sparse_1", "cross_window_pulse"])
def test_degenerate_first_attempt_retries_same_preset_only_once(speech_delivery, style, kind):
    """首包前重试沿原参数且最多一次，失败音频不混入成功声音，三种语气约束一致。"""
    models = speech_delivery.models
    method = (
        models._voice.inference_zero_shot
        if style == "NEUTRAL"
        else models._voice.inference_instruct2
    )
    method.side_effect = [_chunks([]), _chunks([])]
    models._pcm_chunks = Mock(side_effect=[_chunks([_bad_signal(kind)]), _chunks([_tone()])])
    result = list(
        models.synthesize_stream("受控重试样本", style=getattr(speech_delivery.style_type, style))
    )
    assert result == [_tone()]
    assert method.call_count == 2
    assert method.call_args_list[0] == method.call_args_list[1]
    assert not models._tts_lock.locked()


def test_repeated_weak_signal_fails_http_before_success_headers(speech_delivery):
    """两次弱噪仍走原 503 失败契约，不以有字节的 HTTP200 误报朗读成功或泄漏许可。"""
    models = speech_delivery.models
    models._voice.inference_zero_shot.side_effect = [_chunks([]), _chunks([])]
    models._pcm_chunks = Mock(
        side_effect=[_chunks([_bad_signal("weak_noise")]), _chunks([_bad_signal("weak_noise")])]
    )
    speech_delivery.api.app.state.service = speech_delivery.service_type(models, Mock())
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "cosyvoice3-0.5b",
                "voice": "default",
                "input": "受控弱音样本",
                "response_format": "pcm",
            },
        )
    finally:
        client.close()
    assert response.status_code == 503
    assert "受控弱音样本" not in response.text
    assert models._voice.inference_zero_shot.call_count == 2
    assert not models._tts_lock.locked()
    assert not speech_delivery.api.app.state.tts_admission.locked()


def test_gate_logs_once_per_decision_with_only_finite_scalars(speech_delivery, caplog):
    """默认日志消息应直接含可观察标量，且每判定仅一次，不依赖 extra 或每帧日志。"""
    module = speech_delivery.implementation
    assert list(module._audible_pcm(_chunks([_tone(), _tone()]), None))
    with pytest.raises(module.SilentSynthesisError):
        next(module._audible_pcm(_chunks([_bad_signal("weak_noise")]), None))
    records = [
        record.getMessage() for record in caplog.records if "PCM首包判定" in record.getMessage()
    ]
    assert len(records) == 2
    assert "result=pass reason=sustained" in records[0]
    assert "qualified_run=3 observed_samples=1440" in records[0]
    assert "max_window_rms=" in records[0]
    assert "result=reject reason=eof" in records[1]
    assert "qualified_run=0 observed_samples=2400" in records[1]
