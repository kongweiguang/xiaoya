"""识别握手与持续收音的预算分开验证，测试只使用内存协议边界。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from xiaoya.infrastructure.streaming_stt import LocalRecognitionStream


def setup_boundary(monkeypatch, phase: str | None, timeout: float) -> SimpleNamespace:
    """分别挂起升级和 ready，避免把外层测试超时误当成适配器自己的连接预算。"""
    entered = asyncio.Event()
    blocked = asyncio.Event()
    detector = SimpleNamespace(aclose=AsyncMock())
    socket = SimpleNamespace(send_json=AsyncMock())

    async def connect(*_args):
        """兼容标准库上下文登记的绑定参数，升级前取消不伪造 socket 所有权。"""
        if phase == "upgrade":
            entered.set()
            await blocked.wait()
        return socket

    async def ready():
        """服务握手属于同一 setup 预算，已经取得的 socket 必须在失败后回收。"""
        if phase == "ready":
            entered.set()
            await blocked.wait()
        return {"type": "ready"}

    socket.receive_json = AsyncMock(side_effect=ready)
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(side_effect=connect)
    connection.__aexit__ = AsyncMock(return_value=False)
    client = SimpleNamespace(ws_connect=Mock(return_value=connection))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=client)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "xiaoya.infrastructure.streaming_stt.aiohttp.ClientSession", lambda: session
    )
    stream = LocalRecognitionStream.__new__(LocalRecognitionStream)
    stream._recognizer = SimpleNamespace(
        vad=SimpleNamespace(stream=lambda: detector),
        url="ws://private.invalid/v1/audio/transcriptions/stream",
        api_key="diagnostic-placeholder",
        model="paraformer-streaming",
    )
    stream._conn_options = SimpleNamespace(timeout=timeout)
    stream._language = "zh"
    return SimpleNamespace(
        stream=stream,
        entered=entered,
        detector=detector,
        socket=socket,
        connection=connection,
        client=client,
        session=session,
    )


@pytest.mark.parametrize("phase", ["upgrade", "ready"])
async def test_stalled_setup_times_out_and_closes_owned_resources(monkeypatch, phase: str) -> None:
    """外层看门狗不取消被测任务，只有适配器自己超时才算正确回收，且不重试。"""
    boundary = setup_boundary(monkeypatch, phase, timeout=0.03)
    task = asyncio.create_task(boundary.stream._run())
    try:
        await asyncio.wait_for(boundary.entered.wait(), 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 1)
        assert task.done(), "外层看门狗超时不能代替连接自身的超时"
        boundary.detector.aclose.assert_awaited_once()
        boundary.session.__aexit__.assert_awaited_once()
        boundary.client.ws_connect.assert_called_once()
        if phase == "ready":
            boundary.connection.__aexit__.assert_awaited_once()
        else:
            boundary.connection.__aexit__.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["upgrade", "ready"])
async def test_cancelled_setup_preserves_cancellation_and_releases_vad(
    monkeypatch, phase: str
) -> None:
    """用户取消不是超时或空识别，已登记资源仍关闭一次且不创建新的连接。"""
    boundary = setup_boundary(monkeypatch, phase, timeout=10)
    task = asyncio.create_task(boundary.stream._run())
    try:
        await asyncio.wait_for(boundary.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        boundary.detector.aclose.assert_awaited_once()
        boundary.session.__aexit__.assert_awaited_once()
        boundary.client.ws_connect.assert_called_once()
        if phase == "ready":
            boundary.connection.__aexit__.assert_awaited_once()
        else:
            boundary.connection.__aexit__.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_ready_stream_outlives_setup_budget_and_cancels_all_consumers(monkeypatch) -> None:
    """成功握手后不限通话时长，结束时发送、接收和 VAD 消费者必须全部停止。"""
    boundary = setup_boundary(monkeypatch, None, timeout=0.03)
    started: set[str] = set()
    stopped: set[str] = set()
    running = asyncio.Event()
    blocked = asyncio.Event()

    async def consume(name: str):
        """三个模拟消费者停在通话阶段，排除 ready 假成功或残留后台任务。"""
        started.add(name)
        if len(started) == 3:
            running.set()
        try:
            await blocked.wait()
        finally:
            stopped.add(name)

    boundary.stream._feed_vad = lambda _detector: consume("feed")
    boundary.stream._send_audio = lambda _detector, _socket: consume("send")
    boundary.stream._receive_text = lambda _socket: consume("receive")
    task = asyncio.create_task(boundary.stream._run())
    try:
        await asyncio.wait_for(running.wait(), 1)
        await asyncio.sleep(0.09)
        assert not task.done(), "setup 的预算不能覆盖持续收音"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == {"feed", "send", "receive"}
        boundary.detector.aclose.assert_awaited_once()
        boundary.connection.__aexit__.assert_awaited_once()
        boundary.session.__aexit__.assert_awaited_once()
        boundary.client.ws_connect.assert_called_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
