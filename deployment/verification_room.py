"""真实接口验收的资源清理与报告写入共用一个退出边界。"""

import asyncio
import io
import wave
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from time import monotonic

import numpy as np
from livekit import api, rtc


async def wait_until(predicate: Callable[[], bool], timeout: float = 60) -> None:
    """房间与工具按同一阶段预算等待真实结果，连接成功不能替代识别、音频或工具完成。"""
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise TimeoutError("房间验收阶段超时")
        await asyncio.sleep(0.05)


async def publish_wav(source: rtc.AudioSource, content: bytes) -> int:
    """两个房间验收共用合成上行与静音收尾，显式拒绝坏容器，不依赖可关闭的 assert。"""
    with wave.open(io.BytesIO(content)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("验收合成音频必须是单声道 16 位 PCM WAV")
        rate = wav.getframerate()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    if not len(pcm):
        raise ValueError("验收合成音频不能为空")
    count = round(len(pcm) * 48000 / rate)
    speech = np.interp(np.arange(count) * rate / 48000, np.arange(len(pcm)), pcm)
    output = np.concatenate((speech.astype(np.int16), np.zeros(48000, dtype=np.int16)))
    for offset in range(0, len(output), 960):
        chunk = output[offset : offset + 960]
        chunk = np.pad(chunk, (0, 960 - len(chunk)))
        await source.capture_frame(
            rtc.AudioFrame(
                data=chunk.tobytes(),
                sample_rate=48000,
                num_channels=1,
                samples_per_channel=len(chunk),
            )
        )
    await source.wait_for_playout()
    return count


async def create_verification_resources(
    url: str, key: str, secret: str
) -> tuple[rtc.Room, rtc.AudioSource, api.LiveKitAPI]:
    """构造后一项失败时仍关闭前面对象，房间连入前的 FFI 与客户端资源也有所有者。"""
    room = rtc.Room()
    source = None
    control = None
    try:
        source = rtc.AudioSource(48000, 1, queue_size_ms=100)
        control = api.LiveKitAPI(url=url, api_key=key, api_secret=secret)
    except BaseException:
        async with verification_cleanup(
            room=room, source=source, control=control, room_name="", created=False, tasks=()
        ):
            pass
        raise
    return room, source, control


async def cancel_readers(tasks: Iterable[asyncio.Task]) -> None:
    """所有读取任务都收到取消，再统一等待，单项异常不能跳过其他流的关闭。"""
    pending = tuple(tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def complete_cleanup(cleanup: asyncio.Task) -> None:
    """退出任务收到重复取消时仍等待同一个清理者，独立资源关闭不会被打断。"""
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


@asynccontextmanager
async def verification_cleanup(
    *,
    room: rtc.Room,
    source: rtc.AudioSource | None,
    control: api.LiveKitAPI | None,
    room_name: str,
    created: bool,
    tasks: Iterable[asyncio.Task],
    remote=None,
) -> AsyncIterator[None]:
    """报告写入失败也尝试每项释放，只删除本次脚本创建的专属房间。"""
    cleanup = AsyncExitStack()
    try:
        if control is not None:
            cleanup.push_async_callback(control.aclose)
        if created and control is not None:
            cleanup.push_async_callback(
                control.room.delete_room, api.DeleteRoomRequest(room=room_name)
            )
        if source is not None:
            cleanup.push_async_callback(source.aclose)
        cleanup.push_async_callback(cancel_readers, tasks)
        cleanup.push_async_callback(room.disconnect)
        if remote is not None:
            cleanup.push_async_callback(remote.aclose)
        yield
    finally:
        await complete_cleanup(asyncio.create_task(cleanup.aclose()))
