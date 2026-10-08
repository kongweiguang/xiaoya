"""运行进程的就绪通知只在 systemd 明确提供接收地址时发送。"""

import os
import socket


def notify_worker_registered(_worker_id: str, _server_info: object) -> None:
    """SDK 注册事件才证明可接单；非 systemd 启动不创建 Unix socket 或推断就绪。"""
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
        notification.connect(address)
        notification.sendall(b"READY=1")
