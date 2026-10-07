"""FunASR 在线识别与 CosyVoice GPU 合成，模型权重必须已存在于本地。"""

import io
import logging
import os
import wave
from collections.abc import Iterator
from threading import Event, Lock

import av
import numpy as np
import torch
from cosyvoice.cli.cosyvoice import CosyVoice3

from local_speech.domain import SpeechAudio, SpeechStyle, Transcription
from local_speech.streaming_asr import ParaformerRecognizer

logger = logging.getLogger(__name__)

_STYLE_INSTRUCTIONS = {
    SpeechStyle.HAPPY: "You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>",
    SpeechStyle.GENTLE: (
        "You are a helpful assistant. Please say a sentence in a very soft voice.<|endofprompt|>"
    ),
}


class LocalSpeechModels:
    """在线识别缓存彼此隔离，合成模型只处理一条文本流以控制显存峰值。"""

    def __init__(self, recognizer: ParaformerRecognizer) -> None:
        """同一参考音色按语气独立缓存；CUDA、解码引擎与自然语音预热失败仍阻止接单。"""
        if not torch.cuda.is_available():
            raise RuntimeError("CosyVoice 需要可用的 CUDA GPU")
        # GPU 推理仍有 CPU 调度；限制线程避免小块合成被过多线程切换拖慢。
        torch.set_num_threads(4)
        self._recognizer = recognizer
        self._tts_lock = Lock()
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
        # 官方库会记录全文；生产只保留异常，不能把用户文案写入持久日志。
        logging.getLogger().setLevel(logging.WARNING)
        for _ in self.synthesize_stream("你好，语音服务已准备就绪。", speed=1.0):
            pass

    def _prepare_style_voices(self, prompt: str) -> None:
        """上游缓存会覆盖调用参数，必须把语气提示一起预存，且不能改写默认音色缓存。"""
        for style, instruction in _STYLE_INSTRUCTIONS.items():
            self._voice.add_zero_shot_spk(instruction, prompt, f"default:{style.value}")

    def close(self) -> None:
        """等待当前合成释放锁后关闭解码子进程，避免服务重启遗留显存占用。"""
        with self._tts_lock:
            if engine := getattr(self._voice.model.llm, "vllm", None):
                engine.engine_core.shutdown()
                if torch.distributed.is_initialized():
                    torch.distributed.destroy_process_group()
                del self._voice.model.llm.vllm
            torch.cuda.empty_cache()

    def transcribe(self, audio: bytes, language: str | None) -> Transcription:
        """批量兼容接口也使用同一个在线模型，不保留旧 Whisper 推理路径。"""
        stream = self._recognizer.create_stream()
        samples = 0
        with av.open(io.BytesIO(audio)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    pcm = converted.to_ndarray().tobytes()
                    samples += len(pcm) // 2
                    stream.accept(pcm)
            for converted in resampler.resample(None):
                pcm = converted.to_ndarray().tobytes()
                samples += len(pcm) // 2
                stream.accept(pcm)
        return Transcription(
            text=stream.accept(b"", final=True), language=language or "zh", duration=samples / 16000
        )

    def synthesize(
        self, text: str, speed: float, *, style: SpeechStyle = SpeechStyle.NEUTRAL
    ) -> SpeechAudio:
        """WAV 和 PCM 共用同一语气与回退边界，避免下载试听与实时对话行为不一致。"""
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            for chunk in self.synthesize_stream(text, speed, style=style):
                wav.writeframes(chunk)
        return SpeechAudio(content=output.getvalue())

    def synthesize_stream(
        self,
        text: str,
        speed: float,
        *,
        style: SpeechStyle = SpeechStyle.NEUTRAL,
        cancelled: Event | None = None,
    ) -> Iterator[bytes]:
        """等锁可取消；GPU 推理仍安全排空，已播放或取消的句子绝不以自然语气重播。"""
        while True:
            if cancelled is not None and cancelled.is_set():
                return
            if self._tts_lock.acquire(timeout=0.05):
                break
        try:
            # 取消与拿锁可能同时发生，必须在启动推理前再检查一次。
            if cancelled is not None and cancelled.is_set():
                return
            emitted = False
            chunks = self._synthesize_pcm(text, speed, style, cancelled)
            try:
                for chunk in chunks:
                    emitted = True
                    yield chunk
            except Exception:
                if (
                    emitted
                    or style == SpeechStyle.NEUTRAL
                    or (cancelled is not None and cancelled.is_set())
                ):
                    raise
                logger.warning("语气合成在首块音频前失败，使用自然语气重试一次")
            else:
                return
            finally:
                # GeneratorExit 不属于 Exception：取消只关闭并排空，绝不进入回退。
                chunks.close()
            yield from self._synthesize_pcm(text, speed, SpeechStyle.NEUTRAL, cancelled)
        finally:
            self._tts_lock.release()

    def _synthesize_pcm(
        self, text: str, speed: float, style: SpeechStyle, cancelled: Event | None = None
    ) -> Iterator[bytes]:
        """取消在工作线程块边界生效，已启动供应商生成器仍正常排空以释放解码缓存。"""
        if cancelled is not None and cancelled.is_set():
            return
        # 上游会把块长从 25 递增至 100 并保存在模型上；下一句须恢复训练块长。
        self._voice.model.token_hop_len = 25
        if style == SpeechStyle.NEUTRAL:
            inference = self._voice.inference_zero_shot(
                text, "", "", zero_shot_spk_id="default", stream=True, speed=speed
            )
        else:
            inference = self._voice.inference_instruct2(
                text,
                _STYLE_INSTRUCTIONS[style],
                "",
                zero_shot_spk_id=f"default:{style.value}",
                stream=True,
                speed=speed,
            )
        produced = False
        try:
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
                        produced = True
                        yield content
            for converted in resampler.resample(None):
                if content := converted.to_ndarray().tobytes():
                    produced = True
                    yield content
            if not produced:
                raise RuntimeError("语音模型没有返回音频")
        finally:
            try:
                # 官方生成器在正常结束时回收解码缓存；停止播放后仍须让当前句结束。
                for _ in inference:
                    pass
            finally:
                inference.close()
