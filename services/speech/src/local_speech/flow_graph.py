"""固定首块声学计算的有界 CUDA 图，变长输入仍走同一个原始模型。"""

from dataclasses import dataclass
from typing import Any

import torch


@dataclass
class CapturedFlow:
    """图拥有静态输入和输出；调用者只能取得复制结果，不能借用下一轮会改写的缓存。"""

    inputs: dict[str, Any]
    graph: Any
    output: Any


class FlowGraph(torch.nn.Module):
    """只捕获启动预热的流式首块和短句两种形状，运行时不扩张图或显存池。"""

    def __init__(self, decoder) -> None:
        """训练参数与固定噪声不变；搬移同一噪声避免捕获中隐式 CPU 上传。"""
        super().__init__()
        self.decoder = decoder
        decoder.rand_noise = decoder.rand_noise.to("cuda")
        self.captures: dict[tuple, CapturedFlow] = {}
        self.capture_short = False
        self.frozen = False

    @torch.inference_mode()
    def forward(self, **kwargs):
        """形状、布局、类型、设备及全部标量均匹配才重放；返回副本避免旧音频被下轮覆盖。"""
        tensors = {name: value for name, value in kwargs.items() if isinstance(value, torch.Tensor)}
        key = tuple(
            (name, tuple(value.shape), tuple(value.stride()), value.dtype, value.device)
            for name, value in tensors.items()
        ) + tuple((name, value) for name, value in kwargs.items() if name not in tensors)
        key += (torch.is_autocast_enabled("cuda"), torch.get_autocast_dtype("cuda"))
        capture = self.captures.get(key)
        if capture is None:
            eligible = (kwargs.get("streaming") is True and not self.captures) or (
                self.capture_short and kwargs.get("streaming") is False
            )
            if self.frozen or len(self.captures) >= 2 or not eligible:
                return self.decoder(**kwargs)
            capture = self._capture(kwargs, tensors)
            self.captures[key] = capture
        for name, value in tensors.items():
            capture.inputs[name].copy_(value)
        capture.graph.replay()
        return capture.output.clone(), None

    def _capture(self, kwargs, tensors) -> CapturedFlow:
        """捕获时禁用 AMP 权重缓存，避免外层上下文退出后图引用已释放的临时半精度权重。"""
        inputs = {name: value.clone() for name, value in tensors.items()}
        static = {**kwargs, **inputs}
        current = torch.cuda.current_stream()
        stream = torch.cuda.Stream()
        stream.wait_stream(current)
        # dtype 不变；每个权重转换成为图自己持有的操作，不能借外层 AMP 的缓存地址。
        with torch.autocast(
            "cuda",
            enabled=torch.is_autocast_enabled("cuda"),
            dtype=torch.get_autocast_dtype("cuda"),
            cache_enabled=False,
        ):
            with torch.cuda.stream(stream):
                for _ in range(3):
                    self.decoder(**static)
            current.wait_stream(stream)
            graph = torch.cuda.CUDAGraph()
            # token 解码可在同进程另一线程运行；此图只拥有自己的声学 stream。
            with torch.cuda.graph(graph, stream=stream, capture_error_mode="thread_local"):
                output, cache = self.decoder(**static)
        current.wait_stream(stream)
        if cache is not None:
            raise RuntimeError("当前声学解码器返回了未支持的可变缓存")
        return CapturedFlow(inputs, graph, output)
