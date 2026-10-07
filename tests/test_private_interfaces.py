"""用真实 SDK 和内存 HTTP 验证私有端点、请求格式、转写及音频解码。"""

import json
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest
from livekit import rtc
from livekit.agents.llm import ChatContext
from livekit.plugins import silero

from xiaoya.infrastructure.livekit_conversation import LiveKitVoiceConversation
from xiaoya.infrastructure.settings import Settings


@pytest.fixture
def private_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """所有客户端走内存传输，连 SDK 的 models 预热也只能访问指定的模拟地址。"""
    requests: list[httpx.Request] = []
    audio = rtc.AudioFrame(
        data=b"\x00\x00" * 2400, sample_rate=24000, num_channels=1, samples_per_channel=2400
    ).to_wav_bytes()

    def respond(request: httpx.Request) -> httpx.Response:
        """返回标准协议的最小有效响应，未知路径失败以暴露意外网络依赖。"""
        requests.append(request)
        route = (request.method, request.url.host, request.url.path)
        if route == ("GET", "llm.internal", "/v1/models"):
            return httpx.Response(200, json={"object": "list", "data": []})
        if route == ("GET", "api.deepseek.com", "/models"):
            return httpx.Response(200, json={"object": "list", "data": []})
        if route == ("POST", "stt.internal", "/v1/audio/transcriptions"):
            return httpx.Response(200, json={"text": "你好", "language": "zh", "duration": 0.1})
        if route in {
            ("POST", "llm.internal", "/v1/chat/completions"),
            ("POST", "api.deepseek.com", "/chat/completions"),
        }:
            chunk = {
                "id": "private-chat",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": json.loads(request.content)["model"],
                "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}],
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if route == ("POST", "tts.internal", "/v1/audio/speech"):
            return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})
        raise AssertionError(f"不应访问的私有接口：{route}")

    original_client = httpx.AsyncClient

    class InMemoryClient(original_client):
        """保留真实客户端类型，仅替换 HTTP 传输，避免继承机器上的代理配置。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """替换传输而不修改 SDK 请求生成过程，使协议断言覆盖真实调用。"""
            kwargs["transport"] = httpx.MockTransport(respond)
            kwargs["trust_env"] = False
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", InMemoryClient)
    return requests


@pytest.mark.parametrize("authenticated", [False, True])
async def test_private_stt_llm_tts_http_contracts(
    authenticated: bool,
    private_settings: Settings,
    private_requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证独立私有地址、密钥及实际媒体处理，公共环境变量不得接管任一客户端。"""
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://public.invalid/v1")
    settings = private_settings
    if authenticated:
        settings = replace(
            settings, stt_api_key="stt-key", llm_api_key="llm-key", tts_api_key="tts-key"
        )
    adapter = LiveKitVoiceConversation(room=Mock(), settings=settings, vad=silero.VAD.load())
    try:
        frame = rtc.AudioFrame(
            data=b"\x00\x00" * 1600, sample_rate=16000, num_channels=1, samples_per_channel=1600
        )
        transcript = await adapter._stt.recognize(frame)
        assert transcript.alternatives[0].text == "你好"

        context = ChatContext()
        context.add_message(role="user", content="你好")
        async with adapter._llm.chat(chat_ctx=context) as stream:
            chunks = [chunk async for chunk in stream]
        assert "".join(chunk.delta.content or "" for chunk in chunks if chunk.delta) == "你好"

        async with adapter._tts.synthesize("你好") as stream:
            audio = [chunk async for chunk in stream]
        assert sum(chunk.frame.samples_per_channel for chunk in audio) > 0
        assert all(chunk.frame.sample_rate == 24000 for chunk in audio)
    finally:
        await adapter.close()

    posts = {request.url.host: request for request in private_requests if request.method == "POST"}
    assert set(posts) == {"stt.internal", "llm.internal", "tts.internal"}
    assert {request.url.host for request in private_requests} <= set(posts)
    for service in ("stt", "llm", "tts"):
        request = posts[f"{service}.internal"]
        key = f"{service}-key" if authenticated else "not-required"
        assert request.headers["authorization"] == f"Bearer {key}"

    stt_request = posts["stt.internal"]
    assert "multipart/form-data" in stt_request.headers["content-type"]
    assert b'filename="file.wav"' in stt_request.content
    assert b"whisper-1" in stt_request.content
    assert b"verbose_json" in stt_request.content
    assert b"zh" in stt_request.content
    llm_body = json.loads(posts["llm.internal"].content)
    assert llm_body["model"] == settings.llm_model
    assert llm_body["stream"] is True
    assert llm_body["messages"][0]["content"] == "你好"
    tts_body = json.loads(posts["tts.internal"].content)
    assert tts_body["model"] == settings.tts_model
    assert tts_body["voice"] == settings.tts_voice
    assert tts_body["response_format"] == "wav"
    assert tts_body["input"] == "你好"


async def test_deepseek_stream_uses_explicit_key_and_non_thinking_request(
    private_settings: Settings,
    private_requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 SDK 的内存请求证明协议、密钥和思考选项生效，防止仅修改配置而未传入客户端。"""
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    settings = replace(
        private_settings,
        llm_base_url="https://api.deepseek.com",
        llm_model="deepseek-flash",
        llm_api_key="deepseek-test-key",
    )
    adapter = LiveKitVoiceConversation(room=Mock(), settings=settings, vad=silero.VAD.load())
    try:
        context = ChatContext()
        context.add_message(role="user", content="你好")
        async with adapter._llm.chat(chat_ctx=context) as stream:
            chunks = [chunk async for chunk in stream]
        assert "".join(chunk.delta.content or "" for chunk in chunks if chunk.delta) == "你好"
    finally:
        await adapter.close()
    request = next(item for item in private_requests if item.method == "POST")
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer deepseek-test-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "disabled"}
