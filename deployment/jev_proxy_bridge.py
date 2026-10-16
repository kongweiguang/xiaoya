"""WSL NAT 专用 TCP 转发，只允许指定 WSL 地址连接 Windows 的回环代理。"""

import argparse
import asyncio
import ipaddress
import json
import os
from pathlib import Path


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """分块与背压限制内存；半关闭仍允许上游返回，闲置连接不会永久占用资源。"""
    while True:
        async with asyncio.timeout(60):
            data = await reader.read(65536)
        if not data:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
            return
        writer.write(data)
        await writer.drain()


async def forward(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    allowed_peer: str,
    upstream_port: int,
) -> None:
    """只接受本次 WSL 实例，目标固定回环；不解析、保存或打印任何 HTTP 与 TLS 内容。"""
    upstream_writer = None
    pumps: list[asyncio.Task] = []
    try:
        peer = writer.get_extra_info("peername")
        if not peer or peer[0] != allowed_peer:
            return
        async with asyncio.timeout(5):
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", upstream_port
            )
        pumps = [
            asyncio.create_task(copy_stream(reader, upstream_writer)),
            asyncio.create_task(copy_stream(upstream_reader, writer)),
        ]
        await asyncio.gather(*pumps)
    except (OSError, TimeoutError):
        # 客户端会按原 Jev 故障路径降级；代理桥不重试或切换供应商。
        pass
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        for connection in (writer, upstream_writer):
            if connection is not None:
                connection.close()
                try:
                    await connection.wait_closed()
                except OSError:
                    pass


async def serve(listen_host: str, allowed_peer: str, ready_file: Path) -> None:
    """仅绑定 WSL 网卡；原子发布监听成功回执，避免启动器读到半写文件。"""
    listen = ipaddress.ip_address(listen_host)
    peer = ipaddress.ip_address(allowed_peer)
    if not listen.is_private or listen.is_loopback or not peer.is_private:
        raise ValueError("代理桥只允许私有 WSL 网卡与来源地址")

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """每连接独立回收；允许来源由启动时快照固定，不扩大到整个局域网。"""
        await forward(reader, writer, allowed_peer=allowed_peer, upstream_port=10808)

    async with await asyncio.start_server(connected, listen_host, 10818, limit=65536) as server:
        temporary = ready_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        temporary.replace(ready_file)
        try:
            await server.serve_forever()
        finally:
            ready_file.unlink(missing_ok=True)


def main() -> None:
    """启动器必须提供实际网卡与 WSL 来源，禁止默认全网监听或自行发现其他代理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--allowed-peer", required=True)
    parser.add_argument("--ready-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.listen_host, args.allowed_peer, args.ready_file))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
