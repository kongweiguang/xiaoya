"""systemd 就绪只由公开注册事件确认，Windows 帮助路径没有 Unix socket 依赖。"""

from unittest.mock import Mock

import pytest

from xiaoya.infrastructure import systemd


def test_without_notify_socket_does_not_construct_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机或 CLI 场景没有通知地址，不能因平台缺少 AF_UNIX 而影响启动。"""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    factory = Mock()
    monkeypatch.setattr(systemd.socket, "socket", factory)
    systemd.notify_worker_registered("worker", object())
    factory.assert_not_called()


@pytest.mark.parametrize("address", ["/run/xiaoya-notify", "@xiaoya-notify"])
def test_registration_sends_ready_to_explicit_systemd_socket(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重新注册也重新通知同一服务，支持 systemd 的文件与 abstract 地址形式。"""
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    monkeypatch.setattr(systemd.socket, "AF_UNIX", 1, raising=False)
    connection = Mock()
    factory = Mock(return_value=Mock(__enter__=Mock(return_value=connection), __exit__=Mock()))
    monkeypatch.setattr(systemd.socket, "socket", factory)
    systemd.notify_worker_registered("first", object())
    systemd.notify_worker_registered("reconnected", object())
    expected = "\0" + address[1:] if address.startswith("@") else address
    assert factory.call_count == 2
    connection.connect.assert_called_with(expected)
    assert connection.sendall.call_count == 2
    connection.sendall.assert_called_with(b"READY=1")
