"""用真实 SDK 与内存 HTTP 验证官方 DeepSeek、本地语音契约及实际音频解码。"""

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
    """官方地址也只能走内存传输，假密钥不读取机器凭据，SDK 的预热同样不访问外网。"""
    requests: list[httpx.Request] = []
    frame = rtc.AudioFrame(
        data=b"\x20\x00" * 2400, sample_rate=24000, num_channels=1, samples_per_channel=2400
    )

    def respond(request: httpx.Request) -> httpx.Response:
        """返回标准协议的最小有效响应，未知路径失败以暴露意外网络依赖。"""
        requests.append(request)
        route = (request.method, request.url.host, request.url.path)
        if route in {
            ("GET", "api.deepseek.com", "/models"),
            ("GET", "api.deepseek.com", "/v1/models"),
        }:
            return httpx.Response(200, json={"object": "list", "data": []})
        if route in {
            ("POST", "api.deepseek.com", "/chat/completions"),
            ("POST", "api.deepseek.com", "/v1/chat/completions"),
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
            pcm = json.loads(request.content)["response_format"] == "pcm"
            return httpx.Response(
                200,
                content=frame.data.tobytes() if pcm else frame.to_wav_bytes(),
                headers={"content-type": "audio/pcm" if pcm else "audio/wav"},
            )
        raise AssertionError(f"不应访问的模型接口：{route}")

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
@pytest.mark.parametrize("response_format", ["wav", "pcm"])
async def test_deepseek_and_private_tts_http_contracts(
    authenticated: bool,
    response_format: str,
    private_settings: Settings,
    private_requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """假密钥只走内存；两种音频须保留样本，PCM 仅允许 SDK 公开 emitter 的 10ms 结束静音。"""
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://public.invalid/v1")
    settings = replace(private_settings, tts_response_format=response_format)
    if authenticated:
        settings = replace(
            settings, stt_api_key="stt-key", llm_api_key="llm-key", tts_api_key="tts-key"
        )
    adapter = LiveKitVoiceConversation(room=Mock(), settings=settings, vad=silero.VAD.load())
    try:
        adapter._initialize_models()
        context = ChatContext()
        context.add_message(role="user", content="你好")
        async with adapter._llm.chat(chat_ctx=context) as stream:
            chunks = [chunk async for chunk in stream]
        assert "".join(chunk.delta.content or "" for chunk in chunks if chunk.delta) == "你好"

        async with adapter._tts.synthesize("你好") as stream:
            audio = [chunk async for chunk in stream]
        expected = b"\x20\x00" * 2400
        if response_format == "pcm":
            # 当前 SDK 在显式 flush 后追加结束静音标记，它不是服务的重复音频或 WAV 文件头。
            expected += b"\x00\x00" * (24000 // 100)
        assert b"".join(chunk.frame.data.tobytes() for chunk in audio) == expected
        assert all(chunk.frame.sample_rate == 24000 for chunk in audio)
    finally:
        await adapter.close()

    posts = {request.url.host: request for request in private_requests if request.method == "POST"}
    assert set(posts) == {"api.deepseek.com", "tts.internal"}
    assert {request.url.host for request in private_requests} <= set(posts)
    assert posts["api.deepseek.com"].headers["authorization"] == f"Bearer {settings.llm_api_key}"
    assert posts["tts.internal"].headers["authorization"] == f"Bearer {settings.tts_api_key}"

    llm_body = json.loads(posts["api.deepseek.com"].content)
    assert llm_body["model"] == "deepseek-flash"
    assert llm_body["stream"] is True
    assert llm_body["thinking"] == {"type": "disabled"}
    assert llm_body["messages"][0]["content"] == "你好"
    tts_body = json.loads(posts["tts.internal"].content)
    assert tts_body["model"] == settings.tts_model
    assert tts_body["voice"] == settings.tts_voice
    assert tts_body["voice"] == "default"
    assert tts_body["response_format"] == response_format
    assert tts_body["input"] == "你好"


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
async def test_deepseek_stream_uses_explicit_key_and_non_thinking_request(
    base_url: str,
    private_settings: Settings,
    private_requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个官方前缀都通过真实 SDK 生成正确路径、独立假密钥和关闭思考的 SSE 请求。"""
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    settings = replace(
        private_settings,
        llm_base_url=base_url,
        llm_model="deepseek-flash",
        llm_api_key="deepseek-test-key",
    )
    adapter = LiveKitVoiceConversation(room=Mock(), settings=settings, vad=silero.VAD.load())
    try:
        adapter._initialize_models()
        context = ChatContext()
        context.add_message(role="user", content="你好")
        async with adapter._llm.chat(chat_ctx=context) as stream:
            chunks = [chunk async for chunk in stream]
        assert "".join(chunk.delta.content or "" for chunk in chunks if chunk.delta) == "你好"
    finally:
        await adapter.close()
    request = next(item for item in private_requests if item.method == "POST")
    assert str(request.url) == base_url + "/chat/completions"
    assert request.headers["authorization"] == "Bearer deepseek-test-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "disabled"}
