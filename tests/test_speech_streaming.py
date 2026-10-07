"""离线验证增量音频边界、中文切分与取消行为，禁止加载真实语音权重。"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from fastapi.testclient import TestClient
from livekit.agents import stt, utils, vad

from xiaoya.infrastructure.speech_tokenizer import SpeechSentenceTokenizer
from xiaoya.infrastructure.streaming_stt import LocalRecognitionStream, LocalStreamingSTT


@pytest.mark.parametrize("speed", [0, 1.2, float("nan")])
def test_cosyvoice_speed_is_rejected_before_creating_audio(monkeypatch, speed) -> None:
    """上游仅支持原始语速，不能已经发送成功响应后才触发模型断言。"""
    root = Path(__file__).resolve().parents[1] / "services/speech/src"
    monkeypatch.syspath_prepend(str(root))
    from local_speech.application import SpeechService

    models = Mock()
    service = SpeechService(models)
    with pytest.raises(ValueError, match="speed=1.0"):
        service.synthesize_stream("你好", speed)
    with pytest.raises(ValueError, match="speed=1.0"):
        service.synthesize("你好", speed)
    models.synthesize_stream.assert_not_called()
    models.synthesize.assert_not_called()


async def test_pcm_cancel_closes_model_iterator_in_thread(monkeypatch) -> None:
    """只消费首块后取消也应释放模型迭代器，否则下一次合成会被锁阻塞。"""
    root = Path(__file__).resolve().parents[1] / "services/speech/src"
    monkeypatch.syspath_prepend(str(root))
    bootstrap = ModuleType("local_speech.bootstrap")
    bootstrap.create_service = Mock()
    monkeypatch.setitem(sys.modules, "local_speech.bootstrap", bootstrap)
    spec = importlib.util.spec_from_file_location(
        "speech_pcm_under_test", root / "local_speech/api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    released = []

    def model_chunks():
        """模拟持有模型锁的生成器，finally 标志验证取消后的释放边界。"""
        try:
            yield bytes(4800)
            yield bytes(4800)
        finally:
            released.append(True)

    body = module._pcm_body(model_chunks())
    assert len(await anext(body)) == 4800
    await body.aclose()
    assert released == [True]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("你好。有什么可以帮你？", ["你好。", "有什么可以帮你？"]),
        (
            "1. 温度是3.5度，先检查设备。2. 再重启。",
            ["1. 温度是3.5度，先检查设备。", "2. 再重启。"],
        ),
        ("先检查麦克风，再检查网络。", ["先检查麦克风，再检查网络。"]),
        ("答案是“北京。”", ["答案是“北京。"]),
        ("北京。😊！", ["北京。"]),
    ],
)
async def test_chinese_stream_preserves_numbers_and_clauses(text, expected) -> None:
    """逐字到达也不能拆开小数、列表序号与正常逗号，末尾没有标点也应保留。"""
    tokenizer = SpeechSentenceTokenizer()
    stream = tokenizer.stream()
    for character in text:
        stream.push_text(character)
    stream.end_input()
    assert [event.token async for event in stream] == expected
    assert tokenizer.tokenize(text) == expected


async def test_sentence_stream_emits_short_sentence_before_llm_finishes() -> None:
    """短句立即可用而无需等到默认二十字门槛；打断不能提交尚未说出的半句。"""
    stream = SpeechSentenceTokenizer().stream()
    stream.push_text("北京。还有")
    assert (await asyncio.wait_for(anext(stream), 0.1)).token == "北京。"
    await stream.aclose()
    assert [event async for event in stream] == []


async def test_quote_after_sentence_does_not_create_a_punctuation_only_request() -> None:
    """LLM 会把闭引号放入后一个 token，不能为它单独生成一条空音频请求。"""
    stream = SpeechSentenceTokenizer().stream()
    stream.push_text("答案是“北京。")
    assert (await anext(stream)).token == "答案是“北京。"
    stream.push_text("”")
    stream.end_input()
    assert [event async for event in stream] == []


async def test_asr_sends_incremental_frames_without_duplicating_end_buffer() -> None:
    """VAD END 携带完整音频，但此前已经上传，重复发送会造成识别词语重复。"""
    recognizer = LocalStreamingSTT(
        base_url="http://private/v1",
        model="paraformer-streaming",
        api_key="private-key",
        language="zh",
        vad_model=Mock(),
    )
    stream = LocalRecognitionStream.__new__(LocalRecognitionStream)
    stream._event_ch = utils.aio.Chan()
    stream._pending_final = None
    stream._speech_end_time = None
    stream._conn_options = SimpleNamespace(timeout=1)

    async def events():
        """前导缓存和后续新块不同，结尾完整缓存只用于比对重复风险。"""
        for event_type, content in (
            (vad.VADEventType.START_OF_SPEECH, b"start"),
            (vad.VADEventType.INFERENCE_DONE, b"new"),
            (vad.VADEventType.END_OF_SPEECH, b"startnew"),
        ):
            yield SimpleNamespace(
                type=event_type,
                frames=[SimpleNamespace(data=memoryview(content))],
                silence_duration=0.3,
                inference_duration=0.01,
            )

    async def acknowledge(message):
        """模拟服务端确认最终文本，发送任务不依赖真实网络。"""
        if message["type"] == "commit":
            stream._pending_final.set_result(None)

    socket = Mock(send_bytes=AsyncMock(), send_json=AsyncMock(side_effect=acknowledge))
    await stream._send_audio(events(), socket)
    assert [call.args[0] for call in socket.send_bytes.call_args_list] == [b"start", b"new"]
    assert recognizer.url == "ws://private/v1/audio/transcriptions/stream"


async def test_asr_unexpected_disconnect_is_an_error() -> None:
    """空连接不能被当成正常完成，否则 SDK 会保留一个没有转写的假成功轮次。"""

    async def messages():
        """服务端只发临时文字后关闭，没有 done 确认。"""
        yield SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT, data=json.dumps({"type": "partial", "text": "你好"})
        )

    stream = LocalRecognitionStream.__new__(LocalRecognitionStream)
    stream._event_ch = utils.aio.Chan()
    stream._language = "zh"
    with pytest.raises(RuntimeError, match="断开"):
        await stream._receive_text(messages())
    assert (await anext(stream._event_ch)).type == stt.SpeechEventType.INTERIM_TRANSCRIPT


def test_private_streaming_api_preserves_cache_and_pcm_contract(monkeypatch) -> None:
    """内存 WebSocket 验证音频递增、commit 冲刷及下一段新缓存，不连接模型服务。"""
    root = Path(__file__).resolve().parents[1] / "services/speech/src"
    monkeypatch.syspath_prepend(str(root))
    service = Mock()
    streams = [Mock(), Mock()]
    streams[0].accept.side_effect = ["我想", "我想问一下", "我想问一下"]
    service.create_recognition_stream.side_effect = streams
    bootstrap = ModuleType("local_speech.bootstrap")
    bootstrap.create_service = Mock(return_value=service)
    monkeypatch.setitem(sys.modules, "local_speech.bootstrap", bootstrap)
    spec = importlib.util.spec_from_file_location(
        "speech_api_under_test", root / "local_speech/api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as client:
        with client.websocket_connect("/v1/audio/transcriptions/stream") as socket:
            socket.send_json({"model": "paraformer-streaming", "sample_rate": 16000})
            assert socket.receive_json() == {"type": "ready"}
            socket.send_bytes(bytes(3200))
            assert socket.receive_json() == {"type": "partial", "text": "我想"}
            socket.send_bytes(bytes(3200))
            assert socket.receive_json() == {"type": "partial", "text": "我想问一下"}
            socket.send_json({"type": "commit"})
            assert socket.receive_json() == {"type": "final", "text": "我想问一下"}
            socket.send_json({"type": "end"})
            assert socket.receive_json() == {"type": "done"}
    assert streams[0].accept.call_args_list[-1].kwargs == {"final": True}
    assert service.create_recognition_stream.call_count == 2
