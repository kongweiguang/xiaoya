"""模型替身只验证 CUDA 失败边界，默认测试无需下载权重或分配真实 GPU。"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("failure_stage", ["unavailable", "load", "decode", "accelerated_decode"])
def test_cuda_failure_prevents_ready_service_without_cpu_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str
) -> None:
    """CosyVoice 必须在启动时触发真实生成；CUDA 不可用与惰性故障都禁止 CPU 回退。"""
    speech_root = Path(__file__).resolve().parents[1] / "services" / "speech" / "src"
    monkeypatch.syspath_prepend(str(speech_root))
    (tmp_path / "cosyvoice3.yaml").touch()
    prompt = tmp_path / "reference.wav"
    prompt.touch()
    monkeypatch.setenv("SPEECH_TTS_PATH", str(tmp_path))
    monkeypatch.setenv("SPEECH_TTS_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("SPEECH_TTS_PROMPT_TEXT", "本地参考音色")
    monkeypatch.setenv("SPEECH_TTS_NORMALIZER_PATH", str(tmp_path))
    monkeypatch.setenv(
        "SPEECH_TTS_ACCELERATION", "vllm" if failure_stage == "accelerated_decode" else "none"
    )
    for language in ("zh", "en"):
        rules = tmp_path / language / "tn"
        rules.mkdir(parents=True)
        for name in ("tagger.fst", "verbalizer.fst"):
            (rules / name).touch()

    def fail_during_decode():
        """返回惰性故障而非在创建迭代器时失败，覆盖实际首次推理的风险。"""
        yield from ()
        raise RuntimeError("CUDA 无法推理")

    torch = ModuleType("torch")
    torch.cuda = Mock(is_available=Mock(return_value=failure_stage != "unavailable"))
    torch.set_num_threads = Mock()
    torch.distributed = Mock(is_initialized=Mock(return_value=False))
    cosyvoice = ModuleType("cosyvoice.cli.cosyvoice")
    model = Mock()
    model.model.llm = SimpleNamespace()
    model.frontend.text_frontend = "wetext"
    model.inference_zero_shot.return_value = fail_during_decode()
    factory = Mock(return_value=model)
    if failure_stage == "load":
        factory.side_effect = RuntimeError("CUDA 无法加载")
    cosyvoice.CosyVoice3 = factory
    streaming = ModuleType("local_speech.streaming_asr")
    streaming.ParaformerRecognizer = Mock()
    acceleration = ModuleType("local_speech.acceleration")
    engine = Mock()

    def enable_vllm(model, path):
        """初始化引擎后在真实预热阶段故障，资源只能靠构造者的失败清理回收。"""
        model.llm.vllm = engine

    acceleration.enable_vllm = enable_vllm
    for name, module in (
        ("torch", torch),
        ("cosyvoice.cli.cosyvoice", cosyvoice),
        ("local_speech.streaming_asr", streaming),
        ("local_speech.acceleration", acceleration),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "speech_startup_under_test", speech_root / "local_speech" / "infrastructure.py"
    )
    assert spec is not None and spec.loader is not None
    infrastructure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(infrastructure)

    with pytest.raises(RuntimeError, match="CUDA"):
        infrastructure.LocalSpeechModels(Mock())
    if failure_stage == "unavailable":
        factory.assert_not_called()
    else:
        factory.assert_called_once()
        assert factory.call_args.kwargs["fp16"] is True
    if failure_stage == "accelerated_decode":
        engine.engine_core.shutdown.assert_called_once()
        assert not hasattr(model.model.llm, "vllm")
        torch.cuda.empty_cache.assert_called_once()


def test_model_close_is_idempotent_after_engine_shutdown(monkeypatch) -> None:
    """失败清理与服务退出可能相继发生，关闭引擎和进程组都只能执行一次。"""
    speech_root = Path(__file__).resolve().parents[1] / "services/speech/src"
    monkeypatch.syspath_prepend(str(speech_root))
    torch = ModuleType("torch")
    torch.cuda = Mock()
    torch.distributed = Mock(is_initialized=Mock(return_value=True))
    cosyvoice = ModuleType("cosyvoice.cli.cosyvoice")
    cosyvoice.CosyVoice3 = Mock()
    streaming = ModuleType("local_speech.streaming_asr")
    streaming.ParaformerRecognizer = Mock()
    for name, module in (
        ("torch", torch),
        ("cosyvoice.cli.cosyvoice", cosyvoice),
        ("local_speech.streaming_asr", streaming),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "speech_close_under_test", speech_root / "local_speech/infrastructure.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from threading import Lock

    models = module.LocalSpeechModels.__new__(module.LocalSpeechModels)
    models._closed = False
    models._tts_lock = Lock()
    engine = Mock()
    models._voice = SimpleNamespace(model=SimpleNamespace(llm=SimpleNamespace(vllm=engine)))
    models.close()
    models.close()
    engine.engine_core.shutdown.assert_called_once()
    torch.distributed.destroy_process_group.assert_called_once()
    torch.cuda.empty_cache.assert_called_once()
