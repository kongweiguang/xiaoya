"""离线验证图缓存、输入更新和输出所有权；真实 GPU 数值对照属于显式接口验收。"""

import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


class FlowTensor:
    """数组只模拟存储语义，不伪造声学推理或 GPU 性能。"""

    def __init__(self, values):
        """形状、类型与设备参与键匹配，输入存储由调用者独立拥有。"""
        self.values = np.asarray(values)
        self.shape, self.dtype, self.device = self.values.shape, self.values.dtype, "cuda"

    def stride(self):
        """保留非连续布局信息，避免错误重用仅形状相同的图。"""
        return self.values.strides

    def to(self, _device):
        """初始化搬移不改变数值，真实 CUDA 分配交由显式数值验收。"""
        return self

    def clone(self):
        """复制存储是关键约束，上一条输出不能被下一次重放改写。"""
        return FlowTensor(self.values.copy())

    def copy_(self, other):
        """新请求只更新固定地址的输入，不能沿用上一句内容。"""
        np.copyto(self.values, other.values)


@pytest.mark.parametrize("change", ["shape", "steps", "temperature", "dtype", "layout"])
def test_flow_graph_freezes_cache_and_separates_output_storage(monkeypatch, change) -> None:
    """只捕获预热两形状；新标量或布局回到原模型，旧输出与下一轮静态缓存相互隔离。"""
    source = Path(__file__).resolve().parents[1] / "services/speech/src/local_speech/flow_graph.py"

    def inference_mode():
        """离线只解除 Torch 装饰器依赖，不替代或模拟真实 GPU 计算。"""
        return lambda function: function

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            nn=SimpleNamespace(Module=object),
            Tensor=FlowTensor,
            inference_mode=inference_mode,
            is_autocast_enabled=Mock(return_value=True),
            get_autocast_dtype=Mock(return_value="float16"),
        ),
    )
    spec = importlib.util.spec_from_file_location("flow_graph_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    decoder = Mock(rand_noise=FlowTensor([1.0]), return_value=("native", None))
    model = module.FlowGraph(decoder)

    def capture(_kwargs, tensors):
        """测试图只留独立存储，重放调度和数值推理由真实 GPU 对照另行覆盖。"""
        return module.CapturedFlow(
            {name: value.clone() for name, value in tensors.items()},
            Mock(),
            FlowTensor([[10.0, 20.0]]),
        )

    model._capture = Mock(side_effect=capture)
    values = {"mu": FlowTensor([[1.0, 2.0]]), "n_timesteps": 10, "streaming": True}
    first, _ = model.forward(**values)
    values["mu"] = FlowTensor([[3.0, 4.0]])
    model.forward(**values)
    recorded = next(iter(model.captures.values()))
    np.testing.assert_array_equal(recorded.inputs["mu"].values, [[3.0, 4.0]])
    recorded.output.values[:] = 999
    np.testing.assert_array_equal(first.values, [[10.0, 20.0]])
    recorded.graph.replay.assert_called()
    assert model._capture.call_count == 1
    # 第二个变长流式块不能占用短句槽，只有显式启动短句预热可以捕获。
    assert model.forward(**{**values, "mu": FlowTensor([[1.0]])}) == ("native", None)
    model.capture_short = True
    model.forward(**{**values, "streaming": False})
    model.capture_short = False
    model.frozen = True
    assert len(model.captures) == 2
    changed = dict(values)
    if change == "shape":
        changed["mu"] = FlowTensor([[1.0, 2.0, 3.0]])
    elif change == "steps":
        changed["n_timesteps"] = 8
    elif change == "temperature":
        changed["temperature"] = 0.5
    elif change == "dtype":
        changed["mu"] = FlowTensor(np.array([[1.0, 2.0]], dtype=np.float16))
    else:
        changed["mu"] = FlowTensor(np.arange(4.0).reshape(1, 4)[:, ::2])
    assert model.forward(**changed) == ("native", None)
    assert model._capture.call_count == 2


def test_flow_capture_owns_amp_weights_beyond_outer_context(monkeypatch) -> None:
    """图必须禁用 AMP 权重缓存，HTTP 下一请求会退出旧上下文并换线程，不能借临时权重。"""
    source = Path(__file__).resolve().parents[1] / "services/speech/src/local_speech/flow_graph.py"

    def inference_mode():
        """只消除离线 Torch 装饰器依赖，不模拟 GPU 内核。"""
        return lambda function: function

    stream = Mock()
    graph = Mock()
    autocast = Mock(return_value=nullcontext())
    cuda = SimpleNamespace(
        current_stream=Mock(return_value=stream),
        Stream=Mock(return_value=stream),
        stream=Mock(return_value=nullcontext()),
        CUDAGraph=Mock(return_value=graph),
        graph=Mock(return_value=nullcontext()),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            nn=SimpleNamespace(Module=object),
            Tensor=FlowTensor,
            inference_mode=inference_mode,
            is_autocast_enabled=Mock(return_value=True),
            get_autocast_dtype=Mock(return_value="float16"),
            autocast=autocast,
            cuda=cuda,
        ),
    )
    spec = importlib.util.spec_from_file_location("flow_capture_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    decoder = Mock(rand_noise=FlowTensor([1.0]), return_value=(FlowTensor([2.0]), None))
    model = module.FlowGraph(decoder)
    mu = FlowTensor([[1.0]])
    captured = model._capture({"mu": mu, "n_timesteps": 10}, {"mu": mu})
    autocast.assert_called_once_with("cuda", enabled=True, dtype="float16", cache_enabled=False)
    assert captured.graph is graph
    assert captured.inputs["mu"] is not mu
    assert decoder.call_count == 4
    cuda.graph.assert_called_once_with(graph, stream=stream, capture_error_mode="thread_local")
