"""离线验证加速引擎的失败边界，不下载权重、不启动子进程或连接 GPU。"""

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest


@pytest.mark.parametrize("fails", [False, True])
def test_vllm_releases_original_layers_only_after_engine_is_ready(monkeypatch, fails) -> None:
    """引擎失败不能销毁原权重或静默降级，成功才交出 Transformer 的推理职责。"""
    source = Path(__file__).resolve().parents[1] / "services/speech/src/local_speech"
    spec = importlib.util.spec_from_file_location(
        "speech_acceleration_under_test", source / "acceleration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    layers = object()
    transformer = SimpleNamespace(layers=layers)
    encoder = SimpleNamespace(
        model=transformer, config=SimpleNamespace(max_position_embeddings=32768)
    )
    llm = SimpleNamespace(llm=SimpleNamespace(model=encoder))
    model = SimpleNamespace(llm=llm, device="cuda")
    engine = Mock()
    factory = Mock(return_value=engine)
    if fails:
        factory.side_effect = RuntimeError("CUDA 引擎初始化失败")
    vllm = ModuleType("vllm")
    vllm.EngineArgs = Mock()
    vllm.LLMEngine = SimpleNamespace(from_engine_args=factory)
    vllm.ModelRegistry = Mock()
    torch = ModuleType("torch")
    torch.cuda = Mock()
    exporter = ModuleType("cosyvoice.utils.file_utils")
    exporter.export_cosyvoice2_vllm = Mock()
    monkeypatch.setitem(sys.modules, "vllm", vllm)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "cosyvoice.utils.file_utils", exporter)
    # 防止测试污染后续测试或开发机自己的运行设置。
    for key in ("VLLM_NO_USAGE_STATS", "VLLM_DO_NOT_TRACK", "VLLM_WORKER_MULTIPROC_METHOD"):
        monkeypatch.setenv(key, "before-test")
    if fails:
        with pytest.raises(RuntimeError, match="初始化失败"):
            module.enable_vllm(model, "/local-model")
        assert transformer.layers is layers
        assert not hasattr(llm, "vllm")
        torch.cuda.empty_cache.assert_not_called()
    else:
        module.enable_vllm(model, "/local-model")
        assert llm.vllm is engine
        assert not hasattr(transformer, "layers")
        torch.cuda.empty_cache.assert_called_once()


async def test_speech_shutdown_runs_once_even_when_lifespan_body_fails(monkeypatch) -> None:
    """HTTP 生命周期异常退出也要委托资源释放，不能遗留 vLLM 子进程占用显存。"""
    speech_root = Path(__file__).resolve().parents[1] / "services/speech/src"
    monkeypatch.syspath_prepend(str(speech_root))
    service = Mock()
    bootstrap = ModuleType("local_speech.bootstrap")
    bootstrap.create_service = Mock(return_value=service)
    monkeypatch.setitem(sys.modules, "local_speech.bootstrap", bootstrap)
    spec = importlib.util.spec_from_file_location(
        "speech_lifespan_under_test", speech_root / "local_speech/api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="测试退出"):
        async with module.lifespan(module.app):
            raise RuntimeError("测试退出")
    service.close.assert_called_once()


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
async def test_explicit_llm_diagnostic_validates_sse_and_propagates_failure(
    monkeypatch, private_settings, status, base_url
) -> None:
    """显式诊断只复用当前官方 DeepSeek 契约，错误传播但不构成自动启动的付费操作。"""
    private_settings = replace(private_settings, llm_base_url=base_url)
    source = Path(__file__).resolve().parents[1] / "deployment/diagnose_llm.py"
    spec = importlib.util.spec_from_file_location("llm_diagnostic_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_dotenv", Mock())
    monkeypatch.setattr(module.Settings, "from_environment", Mock(return_value=private_settings))
    requests = []

    def respond(request):
        """用内存传输捕获真实 HTTP 请求，测试不读取本机凭据或连接真实模型。"""
        requests.append(request)
        return httpx.Response(
            status,
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"好"}}]}\n\ndata: [DONE]\n\n',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(module.httpx, "AsyncClient", Mock(return_value=client))
    if status == 503:
        with pytest.raises(httpx.HTTPStatusError):
            await module.diagnose(Path("unused-test-env"))
    else:
        await module.diagnose(Path("unused-test-env"))
    assert len(requests) == 1
    assert str(requests[0].url) == private_settings.llm_base_url + "/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer deepseek-test-key"
    body = json.loads(requests[0].content)
    assert body["stream"] is True
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("failure", ["not-sse", "no-content", "no-finish"])
async def test_explicit_llm_diagnostic_rejects_incomplete_response(
    monkeypatch, private_settings, failure
) -> None:
    """HTTP 200 不能代表语音模型可用，诊断要求正文、SSE 格式与结束帧同时存在。"""
    source = Path(__file__).resolve().parents[1] / "deployment/diagnose_llm.py"
    spec = importlib.util.spec_from_file_location("llm_incomplete_diagnostic_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_dotenv", Mock())
    monkeypatch.setattr(module.Settings, "from_environment", Mock(return_value=private_settings))

    def respond(request):
        """缺一项仍返回成功状态，让测试直接验证契约而不是重测 raise_for_status。"""
        content = 'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json" if failure == "not-sse" else "text/event-stream"
            },
            text=("" if failure == "no-content" else content)
            + ("" if failure == "no-finish" else "data: [DONE]\n\n"),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(module.httpx, "AsyncClient", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="SSE"):
        await module.diagnose(Path("unused-test-env"))
