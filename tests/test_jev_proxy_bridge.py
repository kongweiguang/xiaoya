"""本地 TCP 实测代理桥的双向传输、来源拒绝和关闭，不连接任何模型服务。"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "jev_proxy_bridge", Path(__file__).resolve().parents[1] / "deployment/jev_proxy_bridge.py"
)
assert SPEC is not None and SPEC.loader is not None
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


@pytest.mark.asyncio
async def test_bridge_transfers_both_directions_and_preserves_half_close() -> None:
    """真实回环连接证明半关闭后仍能接收响应；桥接不缓存或改写任何字节。"""
    closed = asyncio.Event()

    async def upstream(reader, writer):
        """等待请求半关闭后才响应，以暴露提前取消另一方向的资源回收错误。"""
        try:
            data = await reader.read()
            writer.write(data[::-1])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as target:
        port = target.sockets[0].getsockname()[1]

        async def connected(reader, writer):
            """使用随机测试端口隔离用户真实 VPN，同时执行生产来源校验与传输函数。"""
            await BRIDGE.forward(reader, writer, allowed_peer="127.0.0.1", upstream_port=port)

        async with await asyncio.start_server(connected, "127.0.0.1", 0) as proxy:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy.sockets[0].getsockname()[1]
            )
            try:
                writer.write(b"proxy-contract")
                await writer.drain()
                writer.write_eof()
                async with asyncio.timeout(3):
                    assert await reader.read() == b"tcartnoc-yxorp"
                    await closed.wait()
            finally:
                writer.close()
                await writer.wait_closed()


@pytest.mark.asyncio
async def test_bridge_rejects_other_peers_before_connecting_upstream(monkeypatch) -> None:
    """即使网卡被其他设备访问，非指定 WSL 地址也不能借桥访问 Windows 代理。"""
    called = False
    real_open = asyncio.open_connection

    async def forbidden_open(*args, **kwargs):
        """禁止分配上游，让来源拒绝的行为与单纯连接失败可被区分。"""
        nonlocal called
        called = True
        raise AssertionError("unexpected upstream connection")

    async def connected(reader, writer):
        """测试仅提供不匹配来源，回收仍使用生产实现。"""
        await BRIDGE.forward(reader, writer, allowed_peer="172.17.5.91", upstream_port=10808)

    async with await asyncio.start_server(connected, "127.0.0.1", 0) as proxy:
        monkeypatch.setattr(BRIDGE.asyncio, "open_connection", forbidden_open)
        reader, writer = await real_open("127.0.0.1", proxy.sockets[0].getsockname()[1])
        try:
            async with asyncio.timeout(3):
                assert await reader.read() == b""
            assert not called
        finally:
            writer.close()
            await writer.wait_closed()
