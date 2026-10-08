"""真实接口验收的资源清理与报告写入共用一个退出边界。"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager

from livekit import api, rtc


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
