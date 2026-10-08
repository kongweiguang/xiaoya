"""用模型替身验证语气契约和取消边界，不下载权重、连接服务或使用 GPU。"""

import asyncio
import importlib.util
import io
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import SpooledTemporaryFile
from threading import Event, Lock, get_ident
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import av
import numpy as np
import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


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
    from local_speech.domain import SpeechStyle

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
    models._closed = False
    models._voice = Mock(sample_rate=24000)
    api.app.state.tts_admission = asyncio.Semaphore(1)
    return SimpleNamespace(
        api=api,
        models=models,
        service_type=SpeechService,
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
    """自然音色与三种模板走同一 PCM 用例，WAV 只包装容器，不另起不可取消推理。"""
    port = Mock()
    port.synthesize_stream.return_value = _pcm_chunks()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
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
    cancellation = port.synthesize_stream.call_args.kwargs["cancelled"]
    assert isinstance(cancellation, Event) and cancellation.is_set()
    port.synthesize_stream.assert_called_once_with(
        "我会陪着你。", style=speech_delivery.style_type(expected_style), cancelled=cancellation
    )
    if response_format == "pcm":
        assert response.content == bytes(480)
    else:
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
            assert wav.getframerate() == 24000
            assert wav.readframes(240) == bytes(480)


@pytest.mark.parametrize("instructions", ["happy", "开心自然地说话", "请自由发挥", " " * 2])
@pytest.mark.parametrize("response_format", ["pcm", "wav"])
def test_invalid_delivery_is_rejected_before_audio_creation(
    speech_delivery, instructions, response_format
) -> None:
    """精确白名单防止自由指令或近似模板被当作有效配置，错误必须出现在 HTTP 成功之前。"""
    port = Mock()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
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
    port.synthesize_stream.assert_not_called()


@pytest.mark.parametrize("missing", ["model", "input", "voice"])
def test_missing_tts_fields_are_400_without_echoing_request(speech_delivery, missing: str) -> None:
    """缺字段和语义校验共用 400，不返回框架的原始 body，也不能进入推理或占用许可。"""
    port = Mock()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    payload = {
        "model": "cosyvoice3-0.5b",
        "voice": "default",
        "input": "private-user-input-must-not-leak",
    }
    del payload[missing]
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post("/v1/audio/speech", json=payload)
    finally:
        client.close()
    assert response.status_code == 400
    assert response.json() == {"detail": "请求参数无效，请检查字段类型和格式"}
    assert "private-user-input-must-not-leak" not in response.text
    port.synthesize_stream.assert_not_called()
    assert not speech_delivery.api.app.state.tts_admission.locked()


@pytest.mark.parametrize(
    "field,value",
    [
        ("input", None),
        ("input", 123),
        ("input", ["private-user-input-must-not-leak"]),
        ("input", {"text": "private-user-input-must-not-leak"}),
        ("model", 123),
        ("voice", None),
        ("response_format", ["pcm"]),
        ("instructions", {"text": "private-instructions-must-not-leak"}),
        ("speed", "1.0"),
        ("speed", True),
    ],
)
def test_wrong_tts_field_types_are_400_before_inference(speech_delivery, field, value) -> None:
    """JSON 错类型不靠隐式转换兜底；校验响应隐藏所有原始输入，尤其不能泄露用户文本。"""
    port = Mock()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    payload = {
        "model": "cosyvoice3-0.5b",
        "voice": "default",
        "input": "private-user-input-must-not-leak",
        field: value,
    }
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post("/v1/audio/speech", json=payload)
    finally:
        client.close()
    assert response.status_code == 400
    assert response.json() == {"detail": "请求参数无效，请检查字段类型和格式"}
    assert "private-user-input-must-not-leak" not in response.text
    assert "private-instructions-must-not-leak" not in response.text
    port.synthesize_stream.assert_not_called()
    assert not speech_delivery.api.app.state.tts_admission.locked()


@pytest.mark.parametrize("body", ['{"input":"private-user-input-must-not-leak",', "", "[]"])
def test_malformed_tts_json_is_400_without_exposing_body(speech_delivery, body: str) -> None:
    """坏 JSON、空 body 或非对象 body 都按同一参数契约失败，不将解析位置和输入回显给客户端。"""
    port = Mock()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post(
            "/v1/audio/speech", content=body, headers={"content-type": "application/json"}
        )
    finally:
        client.close()
    assert response.status_code == 400
    assert response.json() == {"detail": "请求参数无效，请检查字段类型和格式"}
    assert "private-user-input-must-not-leak" not in response.text
    port.synthesize_stream.assert_not_called()
    assert not speech_delivery.api.app.state.tts_admission.locked()


def test_http_transcription_missing_fields_share_safe_400_contract(speech_delivery) -> None:
    """统一异常入口也覆盖独立诊断接口，避免同一音频服务仍残留默认 422 或原始表单输入。"""
    port = Mock()
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post(
            "/v1/audio/transcriptions", data={"model": "private-user-input-must-not-leak"}
        )
    finally:
        client.close()
    assert response.status_code == 400
    assert response.json() == {"detail": "请求参数无效，请检查字段类型和格式"}
    assert "private-user-input-must-not-leak" not in response.text
    port.transcribe.assert_not_called()


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
        assert list(models.synthesize_stream("同一句测试。", style=style))
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
        models.synthesize_stream("私有用户文案不能写入日志", style=speech_delivery.style_type.HAPPY)
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
        list(models.synthesize_stream("你好。", style=speech_delivery.style_type.GENTLE))
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
    stream = models.synthesize_stream("不要重复播放。", style=speech_delivery.style_type.HAPPY)
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
    stream = models.synthesize_stream("被打断的句子。", style=speech_delivery.style_type.GENTLE)
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
    first = models.synthesize_stream("开心一句。", style=speech_delivery.style_type.HAPPY)
    assert next(first)
    started = Event()

    def synthesize_second():
        """独立线程模拟同时请求，无固定休眠，是否进入模型完全由生产锁决定。"""
        started.set()
        return list(models.synthesize_stream("轻柔一句。", style=speech_delivery.style_type.GENTLE))

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


async def test_wav_uses_same_style_and_pcm_contract(speech_delivery) -> None:
    """下载试听仍使用真实 WAV 容器和实时合成参数，避免两条语气实现逐渐偏离。"""
    models = speech_delivery.models
    models._voice.inference_instruct2.return_value = _audio_results(0.25)
    body = speech_delivery.api._pcm_body(
        models.synthesize_stream("试听。", style=speech_delivery.style_type.GENTLE)
    )
    result = await speech_delivery.api._collect_wav(await anext(body), body)
    with wave.open(io.BytesIO(result), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 240
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
    assert list(models.synthesize_stream("不应播放。", cancelled=cancelled)) == []
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
                "不再朗读。", style=speech_delivery.style_type.HAPPY, cancelled=cancelled
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
        "取消中的句子。", style=speech_delivery.style_type.HAPPY, cancelled=cancelled
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
    speech_delivery.api.app.state.service = speech_delivery.service_type(models, Mock())
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
        """首块尚未成功时连成功头也不能发送，断连后无需伪造空音频响应。"""
        sent.append(message)

    request = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 2)
        disconnected.set()
        await asyncio.wait_for(request, 2)
        assert actual_lock.locked()
        assert sent == []
        assert not any(item.get("body") for item in sent)
        models._voice.inference_instruct2.assert_not_called()
        models._voice.inference_zero_shot.assert_not_called()
    finally:
        actual_lock.release()


@pytest.mark.parametrize("response_format", ["pcm", "wav"])
@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_first_audio_failure_is_503_without_success_headers(
    speech_delivery, response_format, failure
) -> None:
    """惰性推理失败和空结果都发生在协议成功前，不能以 HTTP 200 掩盖无音频。"""

    def chunks():
        """故障在首次 next 而非创建流时出现，覆盖 StreamingResponse 原有发送时机。"""
        if failure == "exception":
            raise RuntimeError("模型故障不应向客户端泄漏")
        yield from ()

    port = Mock(synthesize_stream=Mock(return_value=chunks()))
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    client = TestClient(speech_delivery.api.app)
    try:
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "cosyvoice3-0.5b",
                "voice": "default",
                "input": "测试。",
                "response_format": response_format,
            },
        )
    finally:
        client.close()
    assert response.status_code == 503
    assert "模型故障" not in response.text
    port.synthesize_stream.assert_called_once()
    assert not speech_delivery.api.app.state.tts_admission.locked()


async def test_async_admission_waiters_leave_thread_pool_available_for_asr(speech_delivery) -> None:
    """排队只占异步任务，单线程池仍能执行识别；断连撤销等待而不取得持有者许可。"""
    waiting = asyncio.Event()

    class Admission(asyncio.Semaphore):
        """只观察公开 acquire 边界，确定所有请求进入排队后再验证线程池。"""

        def __init__(self):
            """零许可模拟正在输出的上一条语音，不启动真实模型。"""
            super().__init__(0)
            self.waiters = 0

        async def acquire(self):
            """第三条请求等待后通知测试，避免固定 sleep 猜测调度次序。"""
            self.waiters += 1
            if self.waiters == 3:
                waiting.set()
            return await super().acquire()

    admission = Admission()
    started = []

    def chunks():
        """只有首次 next 才意味着模型启动，创建与关闭未启动生成器都不占推理线程。"""
        started.append(True)
        yield bytes(480)

    disconnected = asyncio.Event()

    async def receive():
        """所有排队请求同时断连，旧持有者仍未释放自己的许可。"""
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """排队尚未生成首块时，不应该发出任何 HTTP 响应。"""
        pytest.fail("排队响应提前发送")

    def diagnostic():
        """模拟 ASR 的短线程工作，必须在 TTS 队列未解锁时也能完成。"""
        return "识别仍可工作"

    responses = [
        speech_delivery.api.SpeechResponse(chunks(), Event(), admission, "pcm") for _ in range(3)
    ]
    tasks = [
        asyncio.create_task(response({"type": "http"}, receive, send)) for response in responses
    ]
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert (
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(pool, diagnostic), 2
                )
                == "识别仍可工作"
            )
        assert started == []
    finally:
        disconnected.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert admission.locked()
    assert started == []


async def test_queue_timeout_returns_503_without_starting_model(
    speech_delivery, monkeypatch
) -> None:
    """超时等待不能在稍后获得许可时偷偷合成，许可仍归原来的输出者。"""
    api = speech_delivery.api
    monkeypatch.setattr(api, "TTS_QUEUE_TIMEOUT", 0.01)
    admission = asyncio.Semaphore(0)
    started, sent = [], []

    def chunks():
        """有明确启动标记才能区分取消等待与产生空音频。"""
        started.append(True)
        yield bytes(480)

    async def receive():
        """连接保持在线，503 必须由排队预算触发而非断连。"""
        await asyncio.Future()

    async def send(message):
        """记录 ASGI 头和体，确保实际状态码而非仅检查内部异常。"""
        sent.append(message)

    await api.SpeechResponse(chunks(), Event(), admission, "pcm")({"type": "http"}, receive, send)
    assert sent[0]["status"] == 503
    assert started == [] and admission.locked()


async def test_failure_after_first_pcm_terminates_without_replay(speech_delivery) -> None:
    """已经输出的音频不能重试；失败后的清理必须归还许可并保留错误语义。"""
    sent, closed = [], []

    def chunks():
        """首块可听，后块失败，模拟真实 GPU 流中断。"""
        try:
            yield bytes(480)
            raise RuntimeError("中途失败")
        finally:
            closed.append(True)

    async def receive():
        """保持在线以验证模型错误，而非让断连提前结束测试。"""
        await asyncio.Future()

    async def send(message):
        """实际只允许一次成功头和一块 PCM，没有完整结束或新的成功响应。"""
        sent.append(message)

    admission = asyncio.Semaphore(1)
    response = speech_delivery.api.SpeechResponse(chunks(), Event(), admission, "pcm")
    with pytest.raises(RuntimeError, match="中途失败"):
        await response({"type": "http"}, receive, send)
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [
        200
    ]
    assert [message["body"] for message in sent if message.get("body")] == [bytes(480)]
    assert closed == [True] and not admission.locked()


@pytest.mark.parametrize("response_format", ["pcm", "wav"])
async def test_disconnect_keeps_permit_until_pending_inference_is_closed(
    speech_delivery, response_format
) -> None:
    """ASGI 断连不能提前让下一句进入模型，PCM 首包和 WAV 收集共享安全收尾边界。"""
    executing, finish = Event(), Event()
    cancelled = Event()
    disconnected = asyncio.Event()
    sent, closed = [], []

    def chunks():
        """PCM 在首包、WAV 在第二块暂停，覆盖两种格式不同的发送阶段。"""
        try:
            if response_format == "wav":
                yield bytes(480)
            executing.set()
            assert finish.wait(timeout=2)
            yield bytes(480)
        finally:
            closed.append(True)

    async def receive():
        """测试控制真实 ASGI 断连事件，不使用取消替代整个协议生命周期。"""
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """这两个阶段都不允许尚未完成的 HTTP 音频被发送。"""
        sent.append(message)

    admission = asyncio.Semaphore(1)
    response = speech_delivery.api.SpeechResponse(chunks(), cancelled, admission, response_format)
    request = asyncio.create_task(response({"type": "http"}, receive, send))
    try:
        assert await asyncio.to_thread(executing.wait, 2)
        disconnected.set()
        assert await asyncio.to_thread(cancelled.wait, 2)
        assert admission.locked() and closed == [] and sent == []
    finally:
        finish.set()
    await asyncio.wait_for(request, 2)
    assert closed == [True] and not admission.locked()


@pytest.mark.parametrize("response_format", ["json", "verbose_json"])
async def test_diagnostic_transcription_borrows_file_without_full_copy(
    speech_delivery, response_format
) -> None:
    """应用只检查空文件，HTTP 不全量读取，诊断结果产生后才关闭模型借用的文件。"""
    from local_speech.domain import Transcription

    class AudioFile(io.BytesIO):
        """拒绝整段读取，防止未来把 UploadFile.read 的内存复制重新引入接口。"""

        def read(self, size=-1):
            """实际端口采用分块读取；未指定上限即说明接口违背暂存文件所有权。"""
            assert size >= 0
            return super().read(size)

    audio = AudioFile(bytes(100000))
    seen = []

    def transcribe(file, language, *, cancelled):
        """端口得到同一个文件对象并在线程中消费，不是复制后的 bytes。"""
        assert file is audio and not audio.closed
        assert get_ident() != main_thread
        assert language == "zh" and not cancelled.is_set()
        seen.append(file.read(1600))
        return Transcription("诊断文本", "zh", 0.05)

    async def receive():
        """在线客户端无断连，正常结果必须来自转写任务。"""
        await asyncio.Future()

    main_thread = get_ident()
    port = Mock(transcribe=Mock(side_effect=transcribe))
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    result = await speech_delivery.api.transcribe(
        SimpleNamespace(receive=receive),
        UploadFile(audio),
        "paraformer-streaming",
        language="zh",
        response_format=response_format,
    )
    assert result["text"] == "诊断文本" and seen == [bytes(1600)]
    if response_format == "verbose_json":
        assert result == {"text": "诊断文本", "language": "zh", "duration": 0.05}
    assert audio.closed


@pytest.mark.parametrize("disconnect", [False, True])
async def test_transcription_cancel_waits_for_worker_before_closing_upload(
    speech_delivery, disconnect
) -> None:
    """线程仍在读取时不能关闭暂存文件，硬取消和 ASGI 断连使用相同收尾边界。"""
    started, finish, cancellation_seen = Event(), Event(), Event()
    disconnected = asyncio.Event()
    audio = io.BytesIO(bytes(1600))

    def transcribe(file, language, *, cancelled):
        """工作线程等待取消后仍模拟一段清理，检测文件被提前关闭的竞态。"""
        started.set()
        assert cancelled.wait(timeout=2)
        cancellation_seen.set()
        assert finish.wait(timeout=2)
        assert not file.closed
        raise InterruptedError("取消后的线程退出")

    async def receive():
        """实际协议断连与直接 Task.cancel 分别覆盖，避免仅验证一种服务器调度行为。"""
        await disconnected.wait()
        return {"type": "http.disconnect"}

    port = Mock(transcribe=Mock(side_effect=transcribe))
    speech_delivery.api.app.state.service = speech_delivery.service_type(port, Mock())
    request = asyncio.create_task(
        speech_delivery.api.transcribe(
            SimpleNamespace(receive=receive), UploadFile(audio), "paraformer-streaming"
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if disconnect:
            disconnected.set()
        else:
            request.cancel()
        assert await asyncio.to_thread(cancellation_seen.wait, 2)
        if not disconnect:
            request.cancel()
        assert not audio.closed
    finally:
        finish.set()
    if disconnect:
        assert (await asyncio.wait_for(request, 2)).status_code == 204
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 2)
    assert audio.closed


def test_diagnostic_decoder_stops_at_frame_boundary_when_cancelled(speech_delivery) -> None:
    """真实 PyAV 分帧解码在取消后不再冲刷识别缓存，不需要载入 ONNX 模型。"""
    cancelled = Event()
    stream = Mock()

    def accept(pcm, **kwargs):
        """第一帧识别后取消，余下上传不应继续解码或伪造最终结果。"""
        cancelled.set()

    stream.accept.side_effect = accept
    speech_delivery.models._recognizer = Mock(create_stream=Mock(return_value=stream))
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(32000))
    audio.seek(0)
    with pytest.raises(InterruptedError, match="取消"):
        speech_delivery.models.transcribe(audio, "zh", cancelled=cancelled)
    stream.accept.assert_called_once()
    assert not audio.closed


@pytest.mark.parametrize("max_size", [1, 1024 * 1024], ids=["disk", "memory"])
def test_diagnostic_decoder_reads_writable_spooled_upload(speech_delivery, max_size) -> None:
    """真实上传以 w+b 暂存，必须显式读容器，不能让 PyAV 根据文件模式误开写出容器。"""
    stream = Mock()
    stream.accept.return_value = "固定合成音频"
    speech_delivery.models._recognizer = Mock(create_stream=Mock(return_value=stream))
    pcm = np.full(3200, 1234, dtype=np.int16).tobytes()
    with SpooledTemporaryFile(max_size=max_size, mode="w+b") as audio:
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        audio.seek(0)
        result = speech_delivery.models.transcribe(audio, "zh", cancelled=Event())
        assert result.text == "固定合成音频" and result.duration == pytest.approx(0.2)
        assert not audio.closed
    calls = stream.accept.call_args_list
    assert b"".join(call.args[0] for call in calls[:-1]) == pcm
    assert calls[-1].args == (b"",) and calls[-1].kwargs == {"final": True}


def test_http_synthetic_wav_roundtrip_uses_real_upload_decoder(speech_delivery) -> None:
    """合成与 multipart 转写贯通真实容器和重采样，仅替换权重推理，不能再用 BytesIO 遮蔽上传模式。"""
    stream = Mock()
    stream.accept.return_value = "固定合成音频"
    speech_delivery.models._recognizer = Mock(create_stream=Mock(return_value=stream))
    speech_delivery.models._voice.inference_zero_shot.return_value = _audio_results(*([0.25] * 10))
    speech_delivery.api.app.state.service = speech_delivery.service_type(
        speech_delivery.models, Mock()
    )
    client = TestClient(speech_delivery.api.app)
    try:
        speech = client.post(
            "/v1/audio/speech",
            json={
                "model": "cosyvoice3-0.5b",
                "voice": "default",
                "input": "固定合成音频",
                "response_format": "wav",
            },
        )
        assert speech.status_code == 200
        with wave.open(io.BytesIO(speech.content), "rb") as wav:
            assert wav.getframerate() == 24000 and wav.getnframes() == 2400
        transcription = client.post(
            "/v1/audio/transcriptions",
            data={"model": "paraformer-streaming", "response_format": "verbose_json"},
            files={"file": ("synthetic.wav", speech.content, "audio/wav")},
        )
    finally:
        client.close()
    assert transcription.status_code == 200, transcription.text
    assert transcription.json() == {
        "text": "固定合成音频",
        "language": "zh",
        "duration": pytest.approx(0.1),
    }
    calls = stream.accept.call_args_list
    decoded = b"".join(call.args[0] for call in calls[:-1])
    assert len(decoded) == 3200 and np.frombuffer(decoded, dtype=np.int16).any()
    assert calls[-1].args == (b"",) and calls[-1].kwargs == {"final": True}


def _video_only_container() -> bytes:
    """用真实 PyAV 生成无音轨容器，避免把解码器异常替身误当作输入契约回归。"""
    output = io.BytesIO()
    with av.open(output, mode="w", format="matroska") as container:
        stream = container.add_stream("ffv1", rate=1)
        stream.width = stream.height = 16
        stream.pix_fmt = "gray"
        frame = av.VideoFrame.from_ndarray(np.zeros((16, 16), dtype=np.uint8), format="gray")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


def test_diagnostic_decoder_rejects_media_without_audio_before_creating_stream(
    speech_delivery,
) -> None:
    """容器有效但没有音轨也是参数错误，不能交给按索引解码后产生内部异常。"""
    speech_delivery.models._recognizer = Mock()
    with SpooledTemporaryFile(mode="w+b") as audio:
        audio.write(_video_only_container())
        audio.seek(0)
        with pytest.raises(ValueError, match="上传文件必须包含音轨"):
            speech_delivery.models.transcribe(audio, "zh", cancelled=Event())
        assert not audio.closed
    speech_delivery.models._recognizer.create_stream.assert_not_called()


def test_http_video_only_upload_returns_safe_400_and_closes_spool(speech_delivery) -> None:
    """真实 multipart 必须安全拒绝无音轨文件，并沿现有清理路径释放暂存文件。"""
    speech_delivery.models._recognizer = Mock()
    speech_delivery.models.transcribe = Mock(wraps=speech_delivery.models.transcribe)
    speech_delivery.api.app.state.service = speech_delivery.service_type(
        speech_delivery.models, Mock()
    )
    client = TestClient(speech_delivery.api.app, raise_server_exceptions=False)
    try:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"model": "paraformer-streaming"},
            files={"file": ("private-filename.mkv", _video_only_container(), "video/x-matroska")},
        )
    finally:
        client.close()
    assert response.status_code == 400
    assert response.json() == {"detail": "上传文件必须包含音轨"}
    assert "private-filename" not in response.text
    assert speech_delivery.models.transcribe.call_args.args[0].closed
    speech_delivery.models._recognizer.create_stream.assert_not_called()


def test_http_transcription_model_failure_remains_server_error(speech_delivery) -> None:
    """精确的媒体校验不能把实际模型故障也改为客户端参数错误或暴露内部信息。"""
    speech_delivery.models._recognizer = Mock(
        create_stream=Mock(side_effect=RuntimeError("private model failure"))
    )
    speech_delivery.models.transcribe = Mock(wraps=speech_delivery.models.transcribe)
    speech_delivery.api.app.state.service = speech_delivery.service_type(
        speech_delivery.models, Mock()
    )
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(3200))
    client = TestClient(speech_delivery.api.app, raise_server_exceptions=False)
    try:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"model": "paraformer-streaming"},
            files={"file": ("fixed.wav", audio.getvalue(), "audio/wav")},
        )
    finally:
        client.close()
    assert response.status_code == 500 and "private model failure" not in response.text
    assert speech_delivery.models.transcribe.call_args.args[0].closed


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        [],
        "private input",
        1,
        True,
        {},
        {"model": [], "sample_rate": 16000},
        {"model": "paraformer-streaming", "sample_rate": 16000.0},
        {"model": "paraformer-streaming", "sample_rate": "16000"},
        {"model": "paraformer-streaming", "sample_rate": True},
        {"model": "paraformer-streaming", "sample_rate": []},
        {"model": "paraformer-streaming", "sample_rate": 16000, "language": []},
        {"model": "paraformer-streaming", "sample_rate": 16000, "language": {}},
        {"model": "paraformer-streaming", "sample_rate": 16000, "language": None},
        {"model": "paraformer-streaming", "sample_rate": 16000, "language": "private input"},
    ],
)
def test_streaming_invalid_configuration_is_safe_policy_error(
    speech_delivery, configuration
) -> None:
    """JSON 对象和字段实际类型都属于私有协议，非法首包不能以 1011 冒充模型故障。"""
    service = Mock()
    speech_delivery.api.app.state.service = service
    client = TestClient(speech_delivery.api.app)
    try:
        with client.websocket_connect("/v1/audio/transcriptions/stream") as socket:
            socket.send_json(configuration)
            assert socket.receive_json() == {
                "type": "error",
                "message": "流式请求参数无效，请检查消息类型、字段和 PCM16 音频",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1008
    finally:
        client.close()
    service.create_recognition_stream.assert_not_called()


@pytest.mark.parametrize(
    "control",
    [
        None,
        [],
        "private input",
        1,
        True,
        {},
        {"type": None},
        {"type": 1},
        {"type": True},
        {"type": []},
        {"type": {}},
        {"type": "private input"},
    ],
)
def test_streaming_invalid_control_is_safe_policy_error(speech_delivery, control) -> None:
    """就绪后的控制消息也必须验证对象结构，不能重建缓存、回显输入或返回内部故障。"""
    stream = Mock()
    service = Mock(create_recognition_stream=Mock(return_value=stream))
    speech_delivery.api.app.state.service = service
    client = TestClient(speech_delivery.api.app)
    try:
        with client.websocket_connect("/v1/audio/transcriptions/stream") as socket:
            socket.send_json({"model": "paraformer-streaming", "sample_rate": 16000})
            assert socket.receive_json() == {"type": "ready"}
            socket.send_json(control)
            assert socket.receive_json() == {
                "type": "error",
                "message": "流式请求参数无效，请检查消息类型、字段和 PCM16 音频",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1008
    finally:
        client.close()
    service.create_recognition_stream.assert_called_once()
    stream.accept.assert_not_called()


@pytest.mark.parametrize("ready", [False, True], ids=["configuration", "control"])
def test_streaming_malformed_json_uses_safe_policy_error(speech_delivery, ready) -> None:
    """JSON 语法错误与合法 JSON 的结构错误共用安全出口，不返回解码异常或原始正文。"""
    service = Mock()
    speech_delivery.api.app.state.service = service
    client = TestClient(speech_delivery.api.app)
    try:
        with client.websocket_connect("/v1/audio/transcriptions/stream") as socket:
            if ready:
                socket.send_json({"model": "paraformer-streaming", "sample_rate": 16000})
                assert socket.receive_json() == {"type": "ready"}
            socket.send_text("{private input")
            assert socket.receive_json() == {
                "type": "error",
                "message": "流式请求参数无效，请检查消息类型、字段和 PCM16 音频",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1008
    finally:
        client.close()
    assert service.create_recognition_stream.call_count == int(ready)


def test_streaming_model_failure_still_uses_internal_error_code(speech_delivery) -> None:
    """输入收紧不能隐藏真实模型异常，合法首包后的模型失败仍以 1011 关闭且不回显细节。"""
    service = Mock(
        create_recognition_stream=Mock(side_effect=RuntimeError("private model failure"))
    )
    speech_delivery.api.app.state.service = service
    client = TestClient(speech_delivery.api.app)
    try:
        with pytest.raises(RuntimeError, match="private model failure"):
            with client.websocket_connect("/v1/audio/transcriptions/stream") as socket:
                socket.send_json({"model": "paraformer-streaming", "sample_rate": 16000})
                with pytest.raises(WebSocketDisconnect) as closed:
                    socket.receive_json()
                assert closed.value.code == 1011
    finally:
        client.close()
    service.create_recognition_stream.assert_called_once()
