"""FunASR 在线识别与 CosyVoice GPU 合成，模型权重必须已存在于本地。"""

import logging
import os
from collections.abc import Iterator
from threading import Event, Lock
from typing import BinaryIO

import av
import numpy as np
import torch
from cosyvoice.cli.cosyvoice import CosyVoice3

from local_speech.domain import SpeechStyle, Transcription
from local_speech.streaming_asr import ParaformerRecognizer

logger = logging.getLogger(__name__)

_STYLE_INSTRUCTIONS = {
    SpeechStyle.HAPPY: "You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>",
    SpeechStyle.GENTLE: (
        "You are a helpful assistant. Please say a sentence in a very soft voice.<|endofprompt|>"
    ),
}


class SilentSynthesisError(RuntimeError):
    """完整合成只有量化噪声时不能假装朗读成功；该错误仅允许首包前重试。"""


def _audible_pcm(chunks: Iterator[bytes], cancelled: Event | None) -> Iterator[bytes]:
    """只拦截全程不超过三个 PCM16 量化单位的结果；有声音时前导停顿与原字节一起放行。"""
    pending = []
    pending_bytes = 0
    audible = False
    try:
        for chunk in chunks:
            if cancelled is not None and cancelled.is_set():
                return
            if audible:
                yield chunk
                continue
            pcm = np.frombuffer(chunk, dtype="<i2")
            if np.max(np.abs(pcm.astype(np.int32)), initial=0) > 3:
                audible = True
                yield from pending
                pending.clear()
                yield chunk
            else:
                pending_bytes += len(chunk)
                # 约22秒的前导静音已超出正常合成；故障模型不能无限积压首包内存。
                if pending_bytes > 2**20:
                    raise SilentSynthesisError("语音模型未产生可播放信号")
                pending.append(chunk)
        if cancelled is not None and cancelled.is_set():
            return
        if not audible:
            if pending:
                raise SilentSynthesisError("语音模型仅返回量化噪声")
            raise RuntimeError("语音模型没有返回音频")
    finally:
        chunks.close()


class LocalSpeechModels:
    """在线识别缓存彼此隔离，合成模型只处理一条文本流以控制显存峰值。"""

    def __init__(self, recognizer: ParaformerRecognizer) -> None:
        """构造期间失败也必须释放已创建引擎，不能等尚未进入的 HTTP lifespan 清理。"""
        self._recognizer = recognizer
        self._tts_lock = Lock()
        self._voice = None
        self._restore_flow = None
        self._closed = False
        try:
            self._initialize_voice()
        except BaseException:
            try:
                self.close()
            except Exception:
                logger.warning("语音初始化失败后的资源释放未完成")
            raise

    def _initialize_voice(self) -> None:
        """规范化、显式加速与预热均为接单条件；固定声学图不改变权重、精度和音频块。"""
        if not torch.cuda.is_available():
            raise RuntimeError("CosyVoice 需要可用的 CUDA GPU")
        # GPU 推理仍有 CPU 调度；限制线程避免小块合成被过多线程切换拖慢。
        torch.set_num_threads(4)
        path = os.environ["SPEECH_TTS_PATH"]
        if not os.path.isfile(os.path.join(path, "cosyvoice3.yaml")):
            raise ValueError("SPEECH_TTS_PATH 必须指向已下载的 CosyVoice3 模型目录")
        normalizer = os.environ["SPEECH_TTS_NORMALIZER_PATH"]
        for language in ("zh", "en"):
            for name in ("tagger.fst", "verbalizer.fst"):
                if not os.path.isfile(os.path.join(normalizer, language, "tn", name)):
                    raise ValueError("SPEECH_TTS_NORMALIZER_PATH 必须包含本地中英文规范化规则")
        self._voice = CosyVoice3(model_dir=path, fp16=True, load_trt=False, load_vllm=False)
        if self._voice.frontend.text_frontend != "wetext":
            raise RuntimeError("CosyVoice 本地文本规范化前端未加载，请执行部署准备脚本")
        prompt = os.environ["SPEECH_TTS_PROMPT_PATH"]
        if not os.path.isfile(prompt):
            raise ValueError("SPEECH_TTS_PROMPT_PATH 必须指向本地参考音色")
        self._voice.add_zero_shot_spk(
            "You are a helpful assistant.<|endofprompt|>" + os.environ["SPEECH_TTS_PROMPT_TEXT"],
            prompt,
            "default",
        )
        self._prepare_style_voices(prompt)
        acceleration = os.environ.get("SPEECH_TTS_ACCELERATION", "none")
        if acceleration == "vllm":
            from local_speech.acceleration import enable_vllm

            enable_vllm(self._voice.model, path)
        elif acceleration != "none":
            raise ValueError("SPEECH_TTS_ACCELERATION 仅支持 none/vllm")
        flow_acceleration = os.environ.get("SPEECH_TTS_FLOW_ACCELERATION", "none")
        if flow_acceleration == "cuda-graph":
            from local_speech.acceleration import enable_flow_graph

            self._restore_flow = enable_flow_graph(self._voice.model)
        elif flow_acceleration != "none":
            raise ValueError("SPEECH_TTS_FLOW_ACCELERATION 仅支持 none/cuda-graph")
        # 官方库会记录全文；生产只保留异常，不能把用户文案写入持久日志。
        logging.getLogger().setLevel(logging.WARNING)
        for _ in self.synthesize_stream("你好，语音服务已准备就绪。"):
            pass
        if flow_acceleration == "cuda-graph":
            graph = self._voice.model.flow.decoder
            graph.capture_short = True
            try:
                for _ in self.synthesize_stream("你好。"):
                    pass
            finally:
                graph.capture_short = False
                graph.frozen = True
            if len(graph.captures) != 2:
                raise RuntimeError("首块和短句声学图未完成预热")

    def _prepare_style_voices(self, prompt: str) -> None:
        """上游缓存会覆盖调用参数，必须把语气提示一起预存，且不能改写默认音色缓存。"""
        for style, instruction in _STYLE_INSTRUCTIONS.items():
            self._voice.add_zero_shot_spk(instruction, prompt, f"default:{style.value}")

    def close(self) -> None:
        """等待在途推理后只关闭一次；启动失败也还原自己的声学图和掩码适配器。"""
        with self._tts_lock:
            if self._closed:
                return
            self._closed = True
            if self._voice is None:
                return
            try:
                if engine := getattr(self._voice.model.llm, "vllm", None):
                    del self._voice.model.llm.vllm
                    try:
                        engine.engine_core.shutdown()
                    finally:
                        if torch.distributed.is_initialized():
                            torch.distributed.destroy_process_group()
            finally:
                if self._restore_flow is not None:
                    self._restore_flow()
                    self._restore_flow = None
                torch.cuda.empty_cache()

    def transcribe(
        self, audio: BinaryIO, language: str | None, *, cancelled: Event
    ) -> Transcription:
        """显式读容器并先确认音轨，避免把非法媒体误作模型故障；暂存文件仍归 HTTP 所有。"""
        samples = 0
        with av.open(audio, mode="r") as container:
            if not container.streams.audio:
                raise ValueError("上传文件必须包含音轨")
            stream = self._recognizer.create_stream()
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                if cancelled.is_set():
                    raise InterruptedError("转写已取消")
                for converted in resampler.resample(frame):
                    if cancelled.is_set():
                        raise InterruptedError("转写已取消")
                    pcm = converted.to_ndarray().tobytes()
                    samples += len(pcm) // 2
                    stream.accept(pcm)
            for converted in resampler.resample(None):
                if cancelled.is_set():
                    raise InterruptedError("转写已取消")
                pcm = converted.to_ndarray().tobytes()
                samples += len(pcm) // 2
                stream.accept(pcm)
        if cancelled.is_set():
            raise InterruptedError("转写已取消")
        return Transcription(
            text=stream.accept(b"", final=True), language=language or "zh", duration=samples / 16000
        )

    def synthesize_stream(
        self,
        text: str,
        *,
        style: SpeechStyle = SpeechStyle.NEUTRAL,
        cancelled: Event | None = None,
    ) -> Iterator[bytes]:
        """无声短句在首包前同参数重试一次；排队与推理可取消，已播放内容绝不重播。"""
        while True:
            if cancelled is not None and cancelled.is_set():
                return
            if self._tts_lock.acquire(timeout=0.05):
                break
        try:
            # 取消与拿锁可能同时发生，必须在启动推理前再检查一次。
            if cancelled is not None and cancelled.is_set():
                return
            if self._closed:
                raise RuntimeError("语音模型已关闭")
            for attempt in range(2):
                emitted = False
                chunks = self._synthesize_pcm(text, style, cancelled)
                try:
                    for chunk in chunks:
                        emitted = True
                        yield chunk
                except SilentSynthesisError:
                    if emitted or attempt or (cancelled is not None and cancelled.is_set()):
                        raise
                    logger.warning("合成只有量化噪声，首包前以相同语气重试一次")
                except Exception:
                    if (
                        emitted
                        or attempt
                        or style == SpeechStyle.NEUTRAL
                        or (cancelled is not None and cancelled.is_set())
                    ):
                        raise
                    logger.warning("语气合成在首块音频前失败，使用自然语气重试一次")
                    yield from self._synthesize_pcm(text, SpeechStyle.NEUTRAL, cancelled)
                    return
                else:
                    return
                finally:
                    # GeneratorExit 不属于 Exception：取消只关闭并排空，绝不进入回退。
                    chunks.close()
        finally:
            self._tts_lock.release()

    def _synthesize_pcm(
        self, text: str, style: SpeechStyle, cancelled: Event | None = None
    ) -> Iterator[bytes]:
        """放行前排除量化噪声；取消在块边界生效，供应商生成器始终排空并释放缓存。"""
        if cancelled is not None and cancelled.is_set():
            return
        # 上游会把块长从 25 递增至 100 并保存在模型上；下一句须恢复训练块长。
        self._voice.model.token_hop_len = 25
        if style == SpeechStyle.NEUTRAL:
            inference = self._voice.inference_zero_shot(
                text, "", "", zero_shot_spk_id="default", stream=True, speed=1.0
            )
        else:
            inference = self._voice.inference_instruct2(
                text,
                _STYLE_INSTRUCTIONS[style],
                "",
                zero_shot_spk_id=f"default:{style.value}",
                stream=True,
                speed=1.0,
            )
        try:
            yield from _audible_pcm(self._pcm_chunks(inference, cancelled), cancelled)
        finally:
            try:
                # 官方生成器在正常结束时回收解码缓存；停止播放后仍须让当前句结束。
                for _ in inference:
                    pass
            finally:
                inference.close()

    def _pcm_chunks(self, inference, cancelled: Event | None) -> Iterator[bytes]:
        """原有转换独立于信号校验，保证合格音频的采样、块划分和前后静音不被改写。"""
        resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
        for result in inference:
            if cancelled is not None and cancelled.is_set():
                return
            samples = result["tts_speech"].detach().cpu().numpy()
            pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.sample_rate = self._voice.sample_rate
            for converted in resampler.resample(frame):
                if content := converted.to_ndarray().tobytes():
                    yield content
        for converted in resampler.resample(None):
            if content := converted.to_ndarray().tobytes():
                yield content
