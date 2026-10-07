"""通过 ONNX 执行 FunASR Paraformer 在线模型，避免与 LLM 争抢显存。"""

from pathlib import Path
from threading import Lock

import numpy as np
import sherpa_onnx


class ParaformerRecognizer:
    """只加载明确指定的本地 int8 权重，不在服务运行时下载或回退其他模型。"""

    def __init__(self, path: str) -> None:
        """禁用模型自己的断句，由 Agent 本地 VAD 决定何时冲刷识别缓存。"""
        root = Path(path)
        self._model = sherpa_onnx.OnlineRecognizer.from_paraformer(
            tokens=str(root / "tokens.txt"),
            encoder=str(root / "encoder.int8.onnx"),
            decoder=str(root / "decoder.int8.onnx"),
            num_threads=4,
            sample_rate=16000,
            enable_endpoint_detection=False,
            provider="cpu",
        )
        self._lock = Lock()
        self.create_stream().accept(bytes(3200), final=True)

    def create_stream(self) -> "ParaformerStream":
        """每条连接分别创建缓存，模型锁只覆盖一次小块推理。"""
        with self._lock:
            stream = self._model.create_stream()
        return ParaformerStream(self._model, stream, self._lock)


class ParaformerStream:
    """接收增量音频而非重复识别前缀，尾块补齐后禁止再次写入。"""

    def __init__(self, model, stream, lock: Lock) -> None:
        """状态留在连接对象里，锁共享以控制服务端模型推理并发。"""
        self._model = model
        self._stream = stream
        self._lock = lock
        self._finished = False

    def accept(self, pcm: bytes, *, final: bool = False) -> str:
        """末尾追加 600ms 无声上下文只用于模型冲刷，不增加用户实际等待时间。"""
        if self._finished:
            raise ValueError("识别流已经结束")
        if len(pcm) % 2:
            raise ValueError("PCM16 必须按完整采样点发送")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        with self._lock:
            self._stream.accept_waveform(16000, samples)
            if final:
                self._stream.accept_waveform(16000, np.zeros(9600, dtype=np.float32))
                self._stream.input_finished()
                self._finished = True
            while self._model.is_ready(self._stream):
                self._model.decode_stream(self._stream)
            return self._model.get_result(self._stream).strip()
