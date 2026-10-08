"""测试 Job 的依赖隔离与启动失败时的回收注册。"""

from unittest.mock import AsyncMock, Mock

import pytest

from xiaoya import bootstrap
from xiaoya.infrastructure.settings import Settings


def test_worker_capacity_is_configured_before_small_prewarm_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算纠正不能意外创建几十个预热进程，也不能改变 SDK 的过载保护。"""
    calls: list[str] = []

    def configure() -> None:
        """记录装配顺序，避免首次 SDK 采样仍使用旧的 CPU 预算。"""
        calls.append("cpu")

    def construct(**kwargs):
        """仅检查公开构造参数，不依赖框架内部字段或运行真实进程。"""
        calls.append("server")
        assert kwargs["setup_fnc"] is bootstrap.prewarm
        assert kwargs["host"] == "127.0.0.1"
        idle = kwargs["num_idle_processes"]
        assert idle.dev_default == 0
        assert idle.prod_default == 2
        assert "load_fnc" not in kwargs
        assert "load_threshold" not in kwargs
        return sentinel

    sentinel = Mock()
    monkeypatch.setattr(bootstrap, "configure_worker_cpu_budget", configure)
    monkeypatch.setattr(bootstrap, "AgentServer", construct)
    assert bootstrap.prepare_server() is sentinel
    assert calls == ["cpu", "server"]
    sentinel.on.assert_called_once_with("worker_registered", bootstrap.notify_worker_registered)


async def test_each_job_gets_its_own_conversation(
    monkeypatch: pytest.MonkeyPatch, private_settings: Settings
) -> None:
    """VAD 可复用，但会话、工具便签与 MCP 都隔离；人物不再由 Python 发布媒体。"""
    monkeypatch.setattr(bootstrap.Settings, "from_environment", Mock(return_value=private_settings))
    factory = Mock(side_effect=[Mock(close=AsyncMock()), Mock(close=AsyncMock())])
    monkeypatch.setattr(bootstrap, "LiveKitVoiceConversation", factory)
    context = Mock()
    context.proc.userdata = {"vad": Mock()}

    first = bootstrap.prepare_conversation(context)
    second = bootstrap.prepare_conversation(context)

    assert first.conversation is not second.conversation
    assert factory.call_args.kwargs["vad"] is context.proc.userdata["vad"]
    assert factory.call_args.kwargs["on_terminal"] is context.shutdown
    assert "avatar" not in factory.call_args.kwargs
    context.room.local_participant.publish_track.assert_not_called()
    assert context.add_shutdown_callback.call_count == 2
    context.add_shutdown_callback.assert_any_call(first.conversation.close)
    context.add_shutdown_callback.assert_any_call(second.conversation.close)
    first_tools = factory.call_args_list[0].kwargs["tools"].tools()
    second_tools = factory.call_args_list[1].kwargs["tools"].tools()
    save = next(tool for tool in first_tools if tool.info.name == "save_note")
    listing = next(tool for tool in second_tools if tool.info.name == "list_notes")
    await save(title="私有", content="仅第一通话可见")
    assert await listing() == []
    assert factory.call_args_list[0].kwargs["mcp"] is not factory.call_args_list[1].kwargs["mcp"]


def test_invalid_settings_prevent_sdk_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少私有配置时提前失败，避免生成无法使用又需要清理的模型资源。"""
    monkeypatch.setattr(
        bootstrap.Settings, "from_environment", Mock(side_effect=ValueError("缺少私有配置"))
    )
    factory = Mock()
    monkeypatch.setattr(bootstrap, "LiveKitVoiceConversation", factory)
    with pytest.raises(ValueError, match="缺少私有配置"):
        bootstrap.prepare_conversation(Mock())
    factory.assert_not_called()


def test_local_turn_model_prewarm_failure_prevents_accepting_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地模型缺失或二进制不兼容时提前失败，不能把纯 VAD 降级伪装成部署成功。"""
    monkeypatch.setattr(bootstrap, "init_eot", Mock(side_effect=RuntimeError("轮次模型加载失败")))
    vad_factory = Mock()
    monkeypatch.setattr(bootstrap.silero.VAD, "load", vad_factory)
    with pytest.raises(RuntimeError, match="轮次模型加载失败"):
        bootstrap.prewarm(Mock())
    vad_factory.assert_not_called()
