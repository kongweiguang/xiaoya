"""用模型替身验证语气契约和取消边界，不下载权重、连接服务或使用 GPU。"""

import asyncio
import importlib.util
import io
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, get_ident
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient


def _load_module(name: str, path: Path) -> ModuleType:
    """每个测试使用独立模块，避免模型替身泄漏到其他启动或 HTTP 测试。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def speech_delivery(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """只替换模型库导入，保留实际应用校验、音频转换和 HTTP 请求处理。"""
    root = Path(__file__).resolve().parents[1] / "services/speech/src"
    monkeypatch.syspath_prepend(str(root))
    from local_speech.application import SpeechService
    from local_speech.domain import SpeechAudio, SpeechStyle

    torch = ModuleType("torch")
    cosyvoice = ModuleType("cosyvoice.cli.cosyvoice")
    cosyvoice.CosyVoice3 = Mock()
    streaming = ModuleType("local_speech.streaming_asr")
    streaming.ParaformerRecognizer = Mock()
    bootstrap = ModuleType("local_speech.bootstrap")
    bootstrap.create_service = Mock()
    for name, module in (
        ("torch", torch),
        ("cosyvoice.cli.cosyvoice", cosyvoice),
        ("local_speech.streaming_asr", streaming),
        ("local_speech.bootstrap", bootstrap),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    infrastructure = _load_module(
        "speech_delivery_models_under_test", root / "local_speech/infrastructure.py"
    )
    api = _load_module("speech_delivery_api_under_test", root / "local_speech/api.py")
    models = infrastructure.LocalSpeechModels.__new__(infrastructure.LocalSpeechModels)
    models._tts_lock = Lock()
    models._voice = Mock(sample_rate=24000)
    return SimpleNamespace(
        api=api,
        models=models,
        service_type=SpeechService,
        audio_type=SpeechAudio,
        style_type=SpeechStyle,
    )


def _audio_result(value: float = 0.25) -> dict[str, Mock]:
    """使用真实浮点采样让测试覆盖 PCM16 转换，张量接口不需要加载 PyTorch。"""
    tensor = Mock()
    tensor.detach.return_value.cpu.return_value.numpy.return_value = np.full(
        (1, 240), value, dtype=np.float32
    )
    return {"tts_speech": tensor}


def _audio_results(*values: float):
    """惰性产出模型块，使首块前失败和已播放后失败能采用相同消费方式。"""
    for value in values:
        yield _audio_result(value)


def _pcm_chunks():
    """HTTP 必须持有可关闭生成器，模拟连接结束后的真实资源回收约定。"""
    yield bytes(480)


@pytest.mark.parametrize(
    "instructions,expected_style",
    [
        (None, "neutral"),
        ("", "neutral"),
        ("自然平静地说话。", "neutral"),
        ("开心自然地说话。", "happy"),
        ("轻柔温和地说话。", "gentle"),
    ],
)
@pytest.mark.parametrize("response_format", ["pcm", "wav"])
def test_http_delivery_templates_reach_model_port(
    speech_delivery, instructions, expected_style, response_format
) -> None:
    """旧请求与三种固定模板走真实用例，未知 SDK 扩展仍可兼容且指令不会混入朗读文本。"""
    port = Mock()
    port.synthesize_stream.return_value = _pcm_chunks()
    port.synthesize.return_value = speech_delivery.audio_type(content=b"wav-audio")
    speech_delivery.api.app.state.service = speech_delivery.service_type(port)
    payload = {
        "model": "cosyvoice3-0.5b",
        "voice": "default",
        "input": "我会陪着你。",
        "response_format": response_format,
        "stream_format": "sse",
    }
    if instructions is not None:
        payload["instructions"] = instructions
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post("/v1/audio/speech", json=payload)
    finally:
        client.close()
    assert response.status_code == 200
    method = port.synthesize_stream if response_format == "pcm" else port.synthesize
    expected = {"style": speech_delivery.style_type(expected_style)}
    if response_format == "pcm":
        cancellation = method.call_args.kwargs["cancelled"]
        assert isinstance(cancellation, Event) and cancellation.is_set()
        expected["cancelled"] = cancellation
    method.assert_called_once_with("我会陪着你。", 1.0, **expected)
    assert response.content == (bytes(480) if response_format == "pcm" else b"wav-audio")


@pytest.mark.parametrize("instructions", ["happy", "开心自然地说话", "请自由发挥", " " * 2])
@pytest.mark.parametrize("response_format", ["pcm", "wav"])
def test_invalid_delivery_is_rejected_before_audio_creation(
    speech_delivery, instructions, response_format
) -> None:
    """精确白名单防止自由指令或近似模板被当作有效配置，错误必须出现在 HTTP 成功之前。"""
    port = Mock()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port)
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "cosyvoice3-0.5b",
                "voice": "default",
                "input": "你好。",
                "response_format": response_format,
                "instructions": instructions,
            },
        )
    finally:
        client.close()
    assert response.status_code == 400
    assert "instructions" in response.json()["detail"]
    port.synthesize.assert_not_called()
    port.synthesize_stream.assert_not_called()


def test_style_cache_uses_same_voice_without_overwriting_neutral(speech_delivery) -> None:
    """模拟上游缓存优先语义，验证真正进入模型的提示变化而不只检查 HTTP 字段。"""
    models = speech_delivery.models
    cache = {"default": ("原有参考文本", "/private/reference.wav")}
    seen = []

    def add_speaker(prompt_text, prompt_wav, speaker_id):
        """供应商公开预缓存接口保存提示和同一参考音色，禁止共享默认键被覆盖。"""
        assert speaker_id not in cache
        cache[speaker_id] = (prompt_text, prompt_wav)

    def infer(text, instruction, prompt, *, zero_shot_spk_id, stream, speed):
        """上游按缓存 ID 取提示；若错误复用 default，本断言会揭示指令被吞掉。"""
        cached_instruction, reference = cache[zero_shot_spk_id]
        assert cached_instruction == instruction
        assert reference == "/private/reference.wav"
        assert text == "同一句测试。"
        assert prompt == "" and stream is True and speed == 1.0
        seen.append((zero_shot_spk_id, cached_instruction))
        return _audio_results(0.25)

    models._voice.add_zero_shot_spk.side_effect = add_speaker
    models._voice.inference_instruct2.side_effect = infer
    models._prepare_style_voices("/private/reference.wav")
    for style in (speech_delivery.style_type.HAPPY, speech_delivery.style_type.GENTLE):
        assert list(models.synthesize_stream("同一句测试。", 1.0, style=style))
    assert cache["default"] == ("原有参考文本", "/private/reference.wav")
    assert seen == [
        (
            "default:happy",
            "You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>",
        ),
        (
            "default:gentle",
            "You are a helpful assistant. Please say a sentence in a very soft voice."
            "<|endofprompt|>",
        ),
    ]
    models._voice.inference_zero_shot.assert_not_called()


@pytest.mark.parametrize("failure", ["factory", "generator", "empty", "conversion"])
def test_style_failure_before_pcm_falls_back_once(speech_delivery, failure, caplog) -> None:
    """创建、首次迭代与转换失败都允许自然回退，但日志不能泄露用户文案。"""
    models = speech_delivery.models

    def failing_results():
        """首块之前故障或无音频都属于未播放，可安全用自然语气重试一次。"""
        if failure == "generator":
            raise RuntimeError("私有用户文案不能写入日志")
        if failure == "conversion":
            yield {"invalid": None}

    if failure == "factory":
        models._voice.inference_instruct2.side_effect = RuntimeError("私有用户文案不能写入日志")
    else:
        models._voice.inference_instruct2.return_value = failing_results()
    models._voice.inference_zero_shot.return_value = _audio_results(0.25)
    chunks = list(
        models.synthesize_stream(
            "私有用户文案不能写入日志", 1.0, style=speech_delivery.style_type.HAPPY
        )
    )
    assert sum(map(len, chunks)) == 480
    models._voice.inference_instruct2.assert_called_once()
    models._voice.inference_zero_shot.assert_called_once_with(
        "私有用户文案不能写入日志", "", "", zero_shot_spk_id="default", stream=True, speed=1.0
    )
    assert "自然语气重试一次" in caplog.text
    assert "私有用户文案不能写入日志" not in caplog.text
    assert not models._tts_lock.locked()


def test_failed_neutral_fallback_propagates_without_retry(speech_delivery) -> None:
    """自然音色也是模型失败时必须向上传播，不能无限重试或伪装成空音频成功。"""
    models = speech_delivery.models
    models._voice.inference_instruct2.side_effect = RuntimeError("语气不可用")
    models._voice.inference_zero_shot.side_effect = RuntimeError("自然合成也不可用")
    with pytest.raises(RuntimeError, match="自然合成也不可用"):
        list(models.synthesize_stream("你好。", 1.0, style=speech_delivery.style_type.GENTLE))
    models._voice.inference_instruct2.assert_called_once()
    models._voice.inference_zero_shot.assert_called_once()
    assert not models._tts_lock.locked()


def test_failure_after_pcm_never_replays_the_sentence(speech_delivery) -> None:
    """用户可能已经听到首块，之后的错误不能回退整句造成重复朗读。"""
    models = speech_delivery.models
    closed = []

    def partial_failure():
        """先产出有效采样再失败，finally 标记能同时验证异常路径释放。"""
        try:
            yield _audio_result()
            raise RuntimeError("中途推理失败")
        finally:
            closed.append(True)

    models._voice.inference_instruct2.return_value = partial_failure()
    stream = models.synthesize_stream("不要重复播放。", 1.0, style=speech_delivery.style_type.HAPPY)
    assert len(next(stream)) == 480
    with pytest.raises(RuntimeError, match="中途推理失败"):
        next(stream)
    assert closed == [True]
    models._voice.inference_zero_shot.assert_not_called()
    assert not models._tts_lock.locked()


@pytest.mark.parametrize("drain_fails", [False, True])
def test_cancellation_drains_style_and_never_starts_fallback(speech_delivery, drain_fails) -> None:
    """打断须让当前模型收尾后释放锁；清理报错也绝不能触发新的自然合成。"""
    models = speech_delivery.models
    events = []

    def long_results():
        """第二块只能由排空流程消费，避免取消测试仅覆盖已经结束的短流。"""
        try:
            yield _audio_result()
            events.append("drained")
            if drain_fails:
                raise RuntimeError("排空失败")
            yield _audio_result()
        finally:
            events.append("closed")

    models._voice.inference_instruct2.return_value = long_results()
    stream = models.synthesize_stream(
        "被打断的句子。", 1.0, style=speech_delivery.style_type.GENTLE
    )
    assert len(next(stream)) == 480
    if drain_fails:
        with pytest.raises(RuntimeError, match="排空失败"):
            stream.close()
    else:
        stream.close()
    assert events == ["drained", "closed"]
    models._voice.inference_zero_shot.assert_not_called()
    assert not models._tts_lock.locked()


def test_concurrent_styles_are_serialized_until_previous_iterator_closes(speech_delivery) -> None:
    """不同 HTTP 线程共用模型锁，旧句被关闭前不能让下一种语气覆盖模型推理条件。"""
    models = speech_delivery.models
    models._voice.inference_instruct2.side_effect = [_audio_results(0.25), _audio_results(0.5)]
    first = models.synthesize_stream("开心一句。", 1.0, style=speech_delivery.style_type.HAPPY)
    assert next(first)
    started = Event()

    def synthesize_second():
        """独立线程模拟同时请求，无固定休眠，是否进入模型完全由生产锁决定。"""
        started.set()
        return list(
            models.synthesize_stream("轻柔一句。", 1.0, style=speech_delivery.style_type.GENTLE)
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(synthesize_second)
        try:
            assert started.wait(timeout=2)
            assert models._voice.inference_instruct2.call_count == 1
        finally:
            first.close()
        assert future.result(timeout=2)
    assert [
        call.kwargs["zero_shot_spk_id"] for call in models._voice.inference_instruct2.call_args_list
    ] == ["default:happy", "default:gentle"]
    assert not models._tts_lock.locked()


def test_wav_uses_same_style_and_pcm_contract(speech_delivery) -> None:
    """下载试听仍使用真实 WAV 容器和实时合成参数，避免两条语气实现逐渐偏离。"""
    models = speech_delivery.models
    models._voice.inference_instruct2.return_value = _audio_results(0.25)
    result = models.synthesize("试听。", 1.0, style=speech_delivery.style_type.GENTLE)
    with wave.open(io.BytesIO(result.content), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 240
    assert result.media_type == "audio/wav"
    assert (
        models._voice.inference_instruct2.call_args.kwargs["zero_shot_spk_id"] == "default:gentle"
    )


@pytest.mark.parametrize("cancel_before_start", [False, True])
def test_cancelled_waiter_never_enters_model(speech_delivery, cancel_before_start) -> None:
    """取消排队不能等当前句释放锁后再偷偷启动推理，持锁者仍应不受影响。"""
    models = speech_delivery.models
    cancelled, started = Event(), Event()
    models._tts_lock.acquire()
    if cancel_before_start:
        cancelled.set()

    def consume_waiter():
        """阻塞工作线程与真实 HTTP 迭代一致，事件控制顺序而不是用固定休眠碰运气。"""
        started.set()
        return list(
            models.synthesize_stream(
                "这条排队语音已取消。",
                1.0,
                style=speech_delivery.style_type.GENTLE,
                cancelled=cancelled,
            )
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume_waiter)
        try:
            assert started.wait(timeout=2)
            cancelled.set()
            assert future.result(timeout=2) == []
            assert models._tts_lock.locked()
            models._voice.inference_instruct2.assert_not_called()
            models._voice.inference_zero_shot.assert_not_called()
        finally:
            models._tts_lock.release()


def test_cancel_racing_with_lock_acquisition_does_not_start_model(speech_delivery) -> None:
    """线程从等待转为持锁时仍可能刚被取消，拿锁后的检查不可省略。"""
    models = speech_delivery.models
    cancelled = Event()

    def acquire(**kwargs):
        """模拟锁刚可用时收到取消，后续只能释放本次已经取得的锁。"""
        cancelled.set()
        return True

    models._tts_lock = Mock(acquire=Mock(side_effect=acquire))
    assert list(models.synthesize_stream("不应播放。", 1.0, cancelled=cancelled)) == []
    models._tts_lock.release.assert_called_once()
    models._voice.inference_zero_shot.assert_not_called()
    models._voice.inference_instruct2.assert_not_called()


def test_cancelled_style_failure_never_starts_neutral_retry(speech_delivery) -> None:
    """即使语气失败发生在首包前，已经取消的请求也不能额外占用模型重试自然语气。"""
    models = speech_delivery.models
    cancelled = Event()

    def fail_during_cancel(*args, **kwargs):
        """模拟供应商在取消信号同时到达时报告故障，错误仍传播而不是静默重播。"""
        cancelled.set()
        raise RuntimeError("取消时模型失败")

    models._voice.inference_instruct2.side_effect = fail_during_cancel
    with pytest.raises(RuntimeError, match="取消时模型失败"):
        list(
            models.synthesize_stream(
                "不再朗读。", 1.0, style=speech_delivery.style_type.HAPPY, cancelled=cancelled
            )
        )
    models._voice.inference_zero_shot.assert_not_called()
    assert not models._tts_lock.locked()


async def test_http_cancel_during_inference_drains_without_replay(speech_delivery) -> None:
    """正在执行的 GPU 块不硬抢占，取消先撤销输出，块返回后排空并释放锁。"""
    models = speech_delivery.models
    cancelled, started, finish_block = Event(), Event(), Event()
    events = []

    def model_results():
        """阻塞模拟 GPU 在途计算，第二块只能由取消后的排空路径消费。"""
        try:
            started.set()
            assert finish_block.wait(timeout=2)
            yield _audio_result()
            events.append("drained")
            yield _audio_result()
        finally:
            events.append("closed")

    models._voice.inference_instruct2.return_value = model_results()
    chunks = models.synthesize_stream(
        "取消中的句子。", 1.0, style=speech_delivery.style_type.HAPPY, cancelled=cancelled
    )
    body = speech_delivery.api._pcm_body(chunks, cancelled)
    request = asyncio.create_task(anext(body))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        request.cancel()
        assert await asyncio.to_thread(cancelled.wait, 2)
        assert models._tts_lock.locked()
    finally:
        finish_block.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 2)
    assert events == ["drained", "closed"]
    assert not models._tts_lock.locked()
    models._voice.inference_zero_shot.assert_not_called()


async def test_repeated_cancel_never_closes_an_executing_iterator(speech_delivery) -> None:
    """外部任务可再次取消 HTTP 清理，close 仍只能在工作线程的 next 返回后执行。"""
    cancelled, executing, finish_next = Event(), Event(), Event()
    main_thread = get_ident()
    closed = []

    class BlockingChunks:
        """显式记录执行区间，直接捕获 generator already executing 的并发根因。"""

        def __iter__(self):
            """测试替身只维护一条迭代状态，不复制生产生成器。"""
            return self

        def __next__(self):
            """只有测试释放计算边界后才允许 next 返回，取消不能代替计算结束。"""
            executing.set()
            try:
                assert finish_next.wait(timeout=2)
                return bytes(480)
            finally:
                executing.clear()

        def close(self):
            """关闭必须在线程池执行，并且不能和下一块读取重叠。"""
            assert not executing.is_set()
            assert get_ident() != main_thread
            closed.append(True)

    body = speech_delivery.api._pcm_body(BlockingChunks(), cancelled)
    request = asyncio.create_task(anext(body))
    try:
        assert await asyncio.to_thread(executing.wait, 2)
        request.cancel()
        assert await asyncio.to_thread(cancelled.wait, 2)
        request.cancel()
        assert closed == []
    finally:
        finish_next.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 2)
    assert closed == [True]


async def test_asgi_disconnect_cancels_waiting_model_before_lock_release(speech_delivery) -> None:
    """真实 StreamingResponse 的取消域须能通知排队线程，不以释放前一个锁掩盖缺陷。"""
    models = speech_delivery.models
    actual_lock, waiting = Lock(), Event()
    actual_lock.acquire()

    def acquire(**kwargs):
        """观察实际锁等待开始，保证断开不是发生在线程尚未启动的简单路径。"""
        waiting.set()
        return actual_lock.acquire(**kwargs)

    models._tts_lock = Mock(acquire=Mock(side_effect=acquire), release=actual_lock.release)
    speech_delivery.api.app.state.service = speech_delivery.service_type(models)
    response = await speech_delivery.api.synthesize(
        speech_delivery.api.SpeechRequest(
            model="cosyvoice3-0.5b",
            voice="default",
            input="排队后断开。",
            response_format="pcm",
            instructions="轻柔温和地说话。",
        )
    )
    disconnected = asyncio.Event()
    sent = []

    async def receive():
        """ASGI 的断开消息触发真实 AnyIO 取消域，覆盖纯 Task.cancel 之外的清理路径。"""
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """响应头可先发送，但取消后不应产生任何音频体。"""
        sent.append(message)

    request = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 2)
        disconnected.set()
        await asyncio.wait_for(request, 2)
        assert actual_lock.locked()
        assert not any(item.get("body") for item in sent)
        models._voice.inference_instruct2.assert_not_called()
        models._voice.inference_zero_shot.assert_not_called()
    finally:
        actual_lock.release()
