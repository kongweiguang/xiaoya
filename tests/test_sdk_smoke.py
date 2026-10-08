"""使用真实 SDK 验证构造与 Windows spawn，保持网络和付费模型关闭。"""

import asyncio
import multiprocessing
from multiprocessing.connection import Connection
from unittest.mock import Mock

import pytest
from livekit import rtc
from livekit.agents import JobExecutorType, JobProcess

from xiaoya.bootstrap import prewarm
from xiaoya.infrastructure.livekit_conversation import LiveKitVoiceConversation
from xiaoya.infrastructure.settings import Settings


async def test_real_sdk_pipeline_can_be_constructed_without_livekit_credentials(
    monkeypatch: pytest.MonkeyPatch, private_settings: Settings
) -> None:
    """云环境变量不能接管本地轮次模型；保留真实预热，覆盖二进制初始化与 SDK 装配。"""
    from livekit.plugins import openai

    monkeypatch.setattr(openai.LLM, "prewarm", Mock())
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIVEKIT_INFERENCE_URL", "https://cloud.invalid")
    monkeypatch.setenv("LIVEKIT_INFERENCE_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("LIVEKIT_INFERENCE_API_SECRET", "must-not-be-inherited")
    process = JobProcess(
        executor_type=JobExecutorType.PROCESS, user_arguments=None, http_proxy=None
    )
    prewarm(process)
    assert process.userdata["vad"].min_silence_duration == 0.30
    adapter = LiveKitVoiceConversation(
        room=rtc.Room(), settings=private_settings, vad=process.userdata["vad"]
    )
    adapter._initialize_models()
    assert adapter._session.turn_detection.model == "turn-detector-v1-mini"
    stream = adapter._session.turn_detection.stream()
    try:
        prediction = await asyncio.wait_for(stream.predict(), timeout=5)
        assert stream.model == "turn-detector-v1-mini"
        assert not stream.is_fallback
        assert prediction.inference_duration is not None
        assert 0 <= prediction.end_of_turn_probability <= 1
    finally:
        await stream.aclose()
        await adapter.close()


def _import_worker_in_child(connection: Connection) -> None:
    """子进程重新导入模块，验证入口和预热回调不是不可序列化的局部闭包。"""
    from xiaoya.interfaces.cli import server, voice_agent

    connection.send((server.setup_fnc.__name__, voice_agent.__name__))
    connection.close()


def test_worker_entrypoint_is_importable_with_windows_spawn() -> None:
    """显式使用 Windows 的 spawn 行为，避免只在主进程中导入成功。"""
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(target=_import_worker_in_child, args=(child_connection,))
    process.start()
    child_connection.close()
    try:
        assert parent_connection.poll(30), "工作进程未能完成入口导入"
        assert parent_connection.recv() == ("prewarm", "voice_agent")
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent_connection.close()
