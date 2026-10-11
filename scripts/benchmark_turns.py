"""六组合声学轮次诊断：显式离线重放或私有 STT，永不测算浏览器首声最优值。"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import time
import unicodedata
import wave
from collections import Counter
from collections.abc import AsyncIterable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from dotenv import dotenv_values
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    FlushSentinel,
    InterruptionOptions,
    TurnHandlingOptions,
    inference,
    llm,
    stt,
    tts,
    vad,
)
from livekit.agents.voice import io
from livekit.local_inference import init_eot
from livekit.plugins import silero

from xiaoya.infrastructure.streaming_stt import LocalStreamingSTT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATERIALS = PROJECT_ROOT / ".tools" / "live2d-verification" / "utterances"
QUESTIONS = PROJECT_ROOT / ".tools" / "turn-benchmark" / "questions" / "manifest.json"
RATE = 16000
FRAME_SAMPLES = 320
AEC_WARMUP_SECONDS = 0.0
GRID = tuple((delay, duration) for delay in (0.60, 0.75, 0.90) for duration in (0.35, 0.50))
QUESTION_SPEC = (
    ("capital", "我想问一下", "中国的首都在哪里？"),
    ("arithmetic", "请帮我算一下", "二加二等于多少？"),
    ("week", "请告诉我", "一个星期有几天？"),
)
REQUIRED_COVERAGE = {
    "latency": 10,
    "complete-pause-control": 6,
    "noisy-latency": 2,
    "noise-only": 2,
    "acoustic-overlap": 5,
    "incomplete-pause": 9,
}


def percentile(values: list[float], percentage: float) -> float | None:
    """空样本保留为空，分位数采用公开的线性插值口径，不让失败看起来更快。"""
    if not 0 <= percentage <= 100 or any(not math.isfinite(value) for value in values):
        raise ValueError("分位数样本必须为有限值")
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentage / 100
    lower, upper = math.floor(index), math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def distribution(values: list[float]) -> dict:
    """保留计数，使小样本和未执行的条件不能被当作完整验收。"""
    return {"count": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def public_turn_options(delay: float, duration: float, detector) -> TurnHandlingOptions:
    """固定其余轮次规则，每组新 Session，禁止热改 SDK 私有状态或自动选择云端检测。"""
    if (delay, duration) not in GRID:
        raise ValueError("只比较已经约定的六组参数")
    return TurnHandlingOptions(
        turn_detection=detector,
        endpointing={"mode": "fixed", "min_delay": delay, "max_delay": 1.10},
        interruption=InterruptionOptions(
            mode="vad",
            enabled=True,
            min_duration=duration,
            min_words=0,
            resume_false_interruption=True,
            false_interruption_timeout=2.0,
        ),
        preemptive_generation={"enabled": True, "preemptive_tts": True},
    )


def load_pcm(path: Path, expected_hash: str | None = None) -> np.ndarray:
    """只使用标注合成的单声道 PCM，摘要与音频格式先校验，转换不调用模型。"""
    raw = path.read_bytes()
    if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError(f"合成素材摘要不匹配：{path.name}")
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("只支持单声道 PCM_S16LE 合成素材")
        source_rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    if not len(samples):
        raise ValueError("音频素材为空")
    if source_rate != RATE:
        count = round(len(samples) * RATE / source_rate)
        samples = np.interp(np.arange(count) * source_rate / RATE, np.arange(len(samples)), samples)
    return np.clip(samples, -32768, 32767).astype("<i2")


def noise_mix(pcm: np.ndarray, snr_db: float, seed: int) -> np.ndarray:
    """固定随机噪声只用于合成声学压力测试，禁止将它称作真人设备背景噪声。"""
    signal = pcm.astype(np.float64)
    active = np.abs(signal) > 200
    rms = float(np.sqrt(np.mean(signal[active] ** 2))) if active.any() else 500.0
    noise = np.random.default_rng(seed).normal(size=len(signal))
    noise *= rms / (10 ** (snr_db / 20)) / np.sqrt(np.mean(noise**2))
    mixed = signal + noise
    # 保留原音量与 SNR；如果素材失真，要求重新选择输入而非静默压缩整个信号。
    if np.mean(np.abs(mixed) > 32767) > 0.005:
        raise ValueError("噪声混合产生过量削波，请降低素材音量或调整固定样例")
    return np.clip(mixed, -32768, 32767).astype("<i2")


def replay_schedule(segments: list[dict]) -> list[dict]:
    """重放事件明示模拟识别延迟，语音起止仍由真实 Silero 决定，不伪造 VAD 结果。"""
    events = []
    for segment in segments:
        start, end = segment["from_seconds"], segment["to_seconds"]
        events.extend(
            [
                {"at": (start + end) / 2 + 0.08, "final": False, "text": segment["text"]},
                {"at": end + 0.32, "final": True, "text": segment["text"], "end": end},
            ]
        )
    return sorted(events, key=lambda event: event["at"])


def acoustic_bounds(pcm: np.ndarray) -> tuple[float, float] | None:
    """TTS 块含边缘静音，以十毫秒窗定位真实信号；边界方法公开而不冒充人工标注。"""
    samples = pcm.astype(np.float64)
    window = RATE // 100
    padded = np.pad(samples, (0, (-len(samples)) % window))
    rms = np.sqrt(np.mean(padded.reshape(-1, window) ** 2, axis=1))
    active = np.flatnonzero(rms >= max(80.0, float(rms.max()) * 0.025))
    return (int(active[0]) / 100, (int(active[-1]) + 1) / 100) if len(active) else None


def make_case(identifier: str, kind: str, pcm: np.ndarray, segments: list[dict]) -> dict:
    """声学时间轴与语义标签绑定到合成素材，尾部静音另加以容纳 SDK 的有限等待。"""
    bounds = acoustic_bounds(pcm) if segments else None
    return {
        "id": identifier,
        "kind": kind,
        "pcm": pcm,
        "segments": segments,
        "expected_text": "".join(segment["text"] for segment in segments),
        "schedule": replay_schedule(segments),
        "last_speech_seconds": bounds[1] if bounds else None,
        "first_acoustic_seconds": bounds[0] if bounds else None,
        "labelled_text_end_seconds": max(
            (segment["to_seconds"] for segment in segments), default=None
        ),
        "acoustic_boundary_method": "10ms RMS >= max(80, peak-window-RMS * .025)",
        "duration_seconds": len(pcm) / RATE,
        "sha256_pcm": hashlib.sha256(pcm.tobytes()).hexdigest(),
        "synthetic_audio": True,
    }


def read_cases(materials: Path, questions_path: Path) -> tuple[list[dict], list[str]]:
    """完整句停顿只作对照；缺少不完整问题时明确封锁最终参数选择，不假造素材标签。"""
    manifest = json.loads((materials / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("synthetic_audio") or manifest.get("contains_user_audio") is not False:
        raise ValueError("需要明确标注不含用户音频的合成素材 manifest")
    items = {item["id"]: item for item in manifest["utterances"]}
    cases = []
    latency_ids = [f"{index:02}-" for index in (1, 2, *range(19, 27))]
    for prefix in latency_ids:
        item = next(item for name, item in items.items() if name.startswith(prefix))
        pcm = load_pcm(materials / item["file"], item["sha256"])
        cases.append(make_case(item["id"], "latency", pcm, item["segments"]))
    for item in items.values():
        if item["type"] == "explicit-pause":
            cases.append(
                make_case(
                    item["id"],
                    "complete-pause-control",
                    load_pcm(materials / item["file"], item["sha256"]),
                    item["segments"],
                )
            )
    spoken = next(case for case in cases if case["id"] == "20-continuous-question")
    for snr in (20, 10):
        cases.append(
            make_case(
                f"question-noise-{snr}db",
                "noisy-latency",
                noise_mix(spoken["pcm"], snr, 20261009 + snr),
                spoken["segments"],
            )
        )
        # 持续噪声不是新的用户尾音，声学尾音仍取同源干净问题句。
        cases[-1]["last_speech_seconds"] = spoken["last_speech_seconds"]
        cases[-1]["first_acoustic_seconds"] = spoken["first_acoustic_seconds"]
    rng = np.random.default_rng(20261009)
    background = rng.normal(0, 250, RATE * 3).astype("<i2")
    impulses = np.zeros(RATE * 3, dtype="<i2")
    for onset in (0.5, 1.5):
        start = int(onset * RATE)
        impulses[start : start + 1600] = rng.normal(0, 3000, 1600).astype("<i2")
    for name, pcm in (("background", background), ("impulses", impulses)):
        cases.append(make_case(f"noise-only-{name}", "noise-only", pcm, []))
    source = next(case for case in cases if case["id"] == "19-continuous-greeting")
    onset = max(0.0, source["first_acoustic_seconds"] - 0.02)
    for length in (0.20, 0.30, 0.40, 0.55, 0.80):
        part = source["pcm"][int(onset * RATE) : int((onset + length) * RATE)]
        pcm = np.concatenate((np.zeros(8000, dtype="<i2"), part))
        # 此裁切只定义声学语音持续时间，不冒充完整“停止”指令或人类意图。
        case = make_case(f"overlap-{length:.2f}s", "acoustic-overlap", pcm, [])
        case["overlap_seconds"] = length
        case["overlap_source_crop_from_seconds"] = onset
        cases.append(case)
    blockers = []
    if not questions_path.is_file():
        blockers.append("缺少三条明确不完整问题的前缀/后缀及 SHA256 标注，暂停质量门未执行")
    else:
        document = json.loads(questions_path.read_text(encoding="utf-8"))
        if (
            document.get("synthetic_audio") is not True
            or document.get("contains_user_audio") is not False
        ):
            raise ValueError("问题素材必须明确标注为合成音频")
        questions = {item["id"]: item for item in document["questions"]}
        for identifier, prefix_text, suffix_text in QUESTION_SPEC:
            item = questions[identifier]
            if (item["prefix_text"], item["suffix_text"]) != (prefix_text, suffix_text):
                raise ValueError("问题语义标签与固定验收问题不一致")
            prefix = load_pcm(questions_path.parent / item["prefix_file"], item["prefix_sha256"])
            suffix = load_pcm(questions_path.parent / item["suffix_file"], item["suffix_sha256"])
            # 清单记录合成输入与声学边界审核；人工收听状态另列，间隔只由本工具插入。
            if item.get("trimmed_and_reviewed") is not True:
                raise ValueError("不完整问题必须确认声学边界与合成输入，不能按文字长度猜切点")
            for pause in (0.40, 0.60, 0.80):
                lead = np.zeros(4000, dtype="<i2")
                pcm = np.concatenate(
                    (lead, prefix, np.zeros(round(pause * RATE), dtype="<i2"), suffix)
                )
                first_end = 0.25 + len(prefix) / RATE
                second_start = first_end + pause
                cases.append(
                    make_case(
                        f"incomplete-{identifier}-{pause:.2f}s",
                        "incomplete-pause",
                        pcm,
                        [
                            {"text": prefix_text, "from_seconds": 0.25, "to_seconds": first_end},
                            {
                                "text": suffix_text,
                                "from_seconds": second_start,
                                "to_seconds": second_start + len(suffix) / RATE,
                            },
                        ],
                    )
                )
    return cases, blockers


class RealtimeInput(io.AudioInput):
    """输入使用真实时间的二十毫秒帧，无 Room、声卡或后台文件注入。"""

    def __init__(self) -> None:
        """有界缓冲保留背压，不能瞬间灌入整段音频后把假时钟当成真实轮次延迟。"""
        super().__init__(label="turn-matrix-input")
        self.frames = asyncio.Queue(maxsize=4)
        self.closed = False
        self.started_mono = None
        self.started_wall = None

    async def __anext__(self):
        """结束信号只终止本输入迭代器，Session 和模型仍由显式关闭负责回收。"""
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def feed(self, pcm: np.ndarray, *, tail: float = 2.5) -> None:
        """绝对单调时钟调度避免逐帧累积漂移，统计起点对应第一帧进入 SDK 输入。"""
        samples = np.concatenate((pcm, np.zeros(round(tail * RATE), dtype="<i2")))
        self.started_mono, self.started_wall = time.monotonic(), time.time()
        for offset in range(0, len(samples), FRAME_SAMPLES):
            if self.closed:
                return
            deadline = self.started_mono + offset / RATE
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            frame = samples[offset : offset + FRAME_SAMPLES]
            frame = np.pad(frame, (0, FRAME_SAMPLES - len(frame)))
            await self.frames.put(
                rtc.AudioFrame(
                    data=frame.tobytes(),
                    sample_rate=RATE,
                    num_channels=1,
                    samples_per_channel=FRAME_SAMPLES,
                )
            )

    async def aclose(self) -> None:
        """撤销背压并唤醒读取者，取消测试不能遗留等待空队列的输入任务。"""
        self.closed = True
        while not self.frames.empty():
            self.frames.get_nowait()
        self.frames.put_nowait(None)


class RealtimePlayback(io.AudioOutput):
    """可暂停内存播放实现真实 Room 的公开能力，输出仅为模拟播放时钟而非扬声器。"""

    def __init__(self, events: list[dict]) -> None:
        """音频与 flush 共用队列；容量只限制帧，同步 flush 永不因满队列丢失。"""
        super().__init__(label="turn-matrix-playout", capabilities=io.AudioOutputCapabilities(True))
        self.events = events
        self.queue = asyncio.Queue()
        self.capacity = asyncio.Semaphore(5)
        self.enabled = asyncio.Event()
        self.enabled.set()
        self.changed = asyncio.Event()
        self.epoch = 0
        self.capturing = None
        self.segments = {}
        self.closed = False
        self.worker = asyncio.create_task(self.play(), name="turn-matrix-playout")

    def record(self, kind: str, **values) -> None:
        """所有播放事件共用单调时钟，报告不持久化 PCM 或模型服务凭据。"""
        self.events.append({"kind": kind, "mono": time.monotonic(), **values})

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """沿用公开基类段计数，输入超过设备缓冲时阻塞而非无限囤积。"""
        await self.capacity.acquire()
        if self.closed:
            self.capacity.release()
            return
        await super().capture_frame(frame)
        if self.capturing is None:
            self.capturing = self.captured_playout_segments
            self.segments[self.capturing] = {"played": 0.0, "finished": False, "started": False}
        self.queue.put_nowait((self.epoch, self.capturing, frame))

    def flush(self) -> None:
        """EOF 只标记合成输入完成；播放完成必须在队列最后一帧真正耗时后报告。"""
        super().flush()
        if self.capturing is not None:
            self.queue.put_nowait((self.epoch, self.capturing, None))
            self.capturing = None

    def finish(self, identifier: int, interrupted: bool) -> None:
        """每个段终点仅消费一次，迟到 flush 和 clear 不得重复通知 SDK。"""
        segment = self.segments[identifier]
        if not segment["finished"]:
            segment["finished"] = True
            self.on_playback_finished(playback_position=segment["played"], interrupted=interrupted)
            self.record("playback_finished", segment=identifier, interrupted=interrupted)

    def clear_buffer(self) -> None:
        """取消同时隔离当前帧与队列，保留暂停只是冻结播放而非取消的语义区别。"""
        super().flush()
        self.epoch += 1
        self.capturing = None
        self.changed.set()
        self.enabled.set()
        while not self.queue.empty():
            _, _, frame = self.queue.get_nowait()
            if frame is not None:
                self.capacity.release()
        for identifier in self.segments:
            self.finish(identifier, True)
        self.record("clear")

    def pause(self) -> None:
        """冻结剩余播放时间并保留已缓冲帧，随后恢复不能补造一个新的段身份。"""
        if self.enabled.is_set():
            self.enabled.clear()
            self.changed.set()
            self.record("pause")

    def resume(self) -> None:
        """只恢复原段，幂等调用不制造额外恢复事件或时钟推进。"""
        if not self.enabled.is_set():
            self.enabled.set()
            self.changed.set()
            self.record("resume")

    async def play_frame(self, epoch: int, identifier: int, frame: rtc.AudioFrame) -> None:
        """逐帧保留未播放时长，pause/clear 可在任意 await 中断且不误报已播放音频。"""
        remaining = frame.duration
        segment = self.segments[identifier]
        while remaining > 0 and epoch == self.epoch and not self.closed:
            await self.enabled.wait()
            if epoch != self.epoch or self.closed:
                break
            if not segment["started"]:
                segment["started"] = True
                self.on_playback_started(created_at=time.time())
                pcm = np.frombuffer(frame.data, dtype="<i2").astype(np.float64)
                self.record("audio_start", segment=identifier, rms=float(np.sqrt(np.mean(pcm**2))))
            self.changed.clear()
            started = time.monotonic()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except TimeoutError:
                pass
            elapsed = min(remaining, time.monotonic() - started)
            if epoch == self.epoch:
                self.on_playback_progressed(
                    started_at=time.time() - elapsed, offset=segment["played"], duration=elapsed
                )
                segment["played"] += elapsed
            remaining -= elapsed

    async def play(self) -> None:
        """唯一播放任务顺序消费帧与段终点，SDK 回调不会直接推进模拟设备时钟。"""
        while True:
            epoch, identifier, frame = await self.queue.get()
            try:
                if epoch != self.epoch:
                    continue
                if frame is None:
                    self.finish(identifier, False)
                else:
                    await self.play_frame(epoch, identifier, frame)
            finally:
                if frame is not None:
                    self.capacity.release()

    async def aclose(self) -> None:
        """先撤销播放，再等待唯一 worker 取消完成，脚本退出不能掩盖设备任务泄漏。"""
        if not self.closed:
            self.closed = True
            self.clear_buffer()
            self.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.worker


class ObservedAudioStream:
    """仅代理公开音频流接口，真实拥有者是 VAD 或识别流，不读 SDK 任务字段。"""

    def __init__(self, stream, events: list[dict], resource_kind: str) -> None:
        """只登记流实例，不访问 SDK 内部任务或改写模型结果。"""
        self.stream, self.events, self.closed = stream, events, False
        self.resource_kind = resource_kind
        events.append({"kind": f"{resource_kind}_stream_created", "mono": time.monotonic()})

    def push_frame(self, frame) -> None:
        """保持原流背压及输入边界，诊断不会自行判定语音。"""
        self.stream.push_frame(frame)

    def flush(self) -> None:
        """沿用真实 VAD 分段重置，不能忽略 SDK 的显式刷新。"""
        self.stream.flush()

    def end_input(self) -> None:
        """输入结束与取消不同，原流仍负责排空推理结果。"""
        self.stream.end_input()

    def __aiter__(self):
        """单消费者直接读原结果，不添加另一个抢占事件的观察者。"""
        return self

    async def __anext__(self):
        """真实推理输出只记录语音边界，逐帧推理事件不膨胀报告。"""
        event = await self.stream.__anext__()
        if self.resource_kind == "vad" and event.type in {
            vad.VADEventType.START_OF_SPEECH,
            vad.VADEventType.END_OF_SPEECH,
        }:
            self.events.append(
                {
                    "kind": event.type.value,
                    "mono": time.monotonic(),
                    "speech_duration": event.speech_duration,
                    "silence_duration": event.silence_duration,
                }
            )
        return event

    async def aclose(self) -> None:
        """关闭成功才登记回收，不将 finally 进入等同于资源释放完成。"""
        if not self.closed:
            await self.stream.aclose()
            self.closed = True
            self.events.append(
                {"kind": f"{self.resource_kind}_stream_closed", "mono": time.monotonic()}
            )

    async def __aenter__(self):
        """支持正式 STT 的公开上下文用法，拥有权仍属于包装流。"""
        return self

    async def __aexit__(self, *_args) -> None:
        """异常退出与正常结束使用同一显式关闭边界。"""
        await self.aclose()


class ObservedVAD(vad.VAD):
    """真实 Silero 复用权重，公开代理单独核对 Session/STT 所创建的流。"""

    def __init__(self, model, events: list[dict]) -> None:
        """能力沿用真实模型，不冒充可更新或可关闭的模型客户端。"""
        super().__init__(capabilities=model.capabilities)
        self.model_backend, self.events = model, events

    def stream(self):
        """每次创建独立真实流，调用者负责关闭，诊断仅记录其边界。"""
        return ObservedAudioStream(self.model_backend.stream(), self.events, "vad")

    @property
    def min_silence_duration(self):
        """SDK 要求最少静音预算，代理仍暴露真实模型的公开属性以保留官方校验。"""
        return self.model_backend.min_silence_duration


class ObservedRecognizer(LocalStreamingSTT):
    """正式私有适配器只增加公开流回收记录，不改变 WebSocket 或识别逻辑。"""

    def __init__(self, events: list[dict], **kwargs) -> None:
        """沿用显式私有配置，每个样本独立客户端边界并立即登记流拥有权。"""
        super().__init__(**kwargs)
        self.events = events

    def stream(self, **kwargs):
        """公开流关闭完成意味着正式适配器 finally 已等待 HTTP/WS 上下文回收。"""
        return ObservedAudioStream(super().stream(**kwargs), self.events, "stt")


class ObservedTurnStream:
    """官方公开预测 Future 是本地诊断入口，Session 本身不会 emit EOT 事件。"""

    def __init__(self, stream, events: list[dict]) -> None:
        """保持官方流协议全部公开成员，禁止读私有 transport、任务或推理缓存。"""
        self.stream, self.events, self.closed = stream, events, False
        events.append({"kind": "eot_stream_created", "mono": time.monotonic()})

    @property
    def model(self):
        """模型标识取官方公开属性，用于证明确实是 v1-mini。"""
        return self.stream.model

    @property
    def provider(self):
        """保留协议能力，不从服务地址推断供应商。"""
        return self.stream.provider

    @property
    def is_fallback(self):
        """本地固定版本仍透传标志，不能隐藏模型退化。"""
        return self.stream.is_fallback

    @property
    def prediction_timeout(self):
        """SDK 超时策略由原流决定，诊断不另外修改推理预算。"""
        return self.stream.prediction_timeout

    async def supports_language(self, language):
        """保持官方语言能力判断，不能为了让中文测试通过而绕过限制。"""
        return await self.stream.supports_language(language)

    async def unlikely_threshold(self, language):
        """采用官方校准阈值，不在诊断中训练或调节概率。"""
        return await self.stream.unlikely_threshold(language)

    async def backchannel_threshold(self, language):
        """保留协议完整性，当前 mini 模型不凭空制造附加概率。"""
        return await self.stream.backchannel_threshold(language)

    def predict(self):
        """只观察原 Future，不另发预测或改变 SDK 的取消与超时顺序。"""
        future = self.stream.predict()

        def finished(result) -> None:
            """有效推理必须有原流提供的 inference_duration，取消默认值不作模型证据。"""
            if not result.cancelled() and result.exception() is None:
                event = result.result()
                self.events.append(
                    {
                        "kind": "eot_prediction",
                        "mono": time.monotonic(),
                        "probability": event.end_of_turn_probability,
                        "inference_duration": event.inference_duration,
                        "model": self.model,
                    }
                )

        future.add_done_callback(finished)
        return future

    def cancel_inference(self, *, timed_out=False) -> None:
        """所有取消仍由官方流执行，观察器没有独立推理任务。"""
        self.stream.cancel_inference(timed_out=timed_out)

    def flush(self, reason=None) -> None:
        """每个 SDK 刷新都原样传递，不能把旧音频携带到新轮次。"""
        self.stream.flush(reason=reason)

    def push_audio(self, frame) -> None:
        """音频只送往原预测流，不增加第二条推理链路。"""
        self.stream.push_audio(frame)

    def end_input(self) -> None:
        """公开输入结束让官方流管理排空，而非直接取消原任务。"""
        self.stream.end_input()

    async def aclose(self) -> None:
        """真实关闭完成才计数，保证矩阵不会把进程结束当作每轮回收。"""
        if not self.closed:
            await self.stream.aclose()
            self.closed = True
            self.events.append({"kind": "eot_stream_closed", "mono": time.monotonic()})


class ObservedTurnDetector(inference.TurnDetector):
    """只覆盖官方公开 stream 工厂，预测、权重、调度都保留原实现。"""

    def __init__(self, events: list[dict]) -> None:
        """明确固定本地版本，诊断不得继承云端推理环境。"""
        super().__init__(version="v1-mini")
        self.events = events

    def stream(self, **kwargs):
        """代理公开流以记录本地预测，避免依赖未公开的 Session host。"""
        return ObservedTurnStream(super().stream(**kwargs), self.events)


class ReplaySTT(stt.STT):
    """只声明官方 STT 能力，事件来自公开 Agent.stt_node，禁止误调用网络插件。"""

    def __init__(self) -> None:
        """SDK 必须知道 STT 已启用才能进入真实转写轮次流程，空模型会跳过回复。"""
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))

    async def _recognize_impl(self, buffer, *, language, conn_options):
        """官方抽象适配器要求这个扩展点；本诊断只用公开节点且不读取 SDK 内部字段。"""
        raise RuntimeError("离线重放只支持公开 stt_node")


class FixedLLM(llm.LLM):
    """SDK 回复路径需要模型能力存在，实际文本由公开节点提供而非任何外部模型。"""

    def chat(self, **kwargs):
        """误入插件调用立即失败，避免诊断悄悄调用付费模型或继承云端配置。"""
        raise RuntimeError("固定回复只支持公开 llm_node")


class FixedTTS(tts.TTS):
    """音频只来自固定诊断 PCM，能力对象不会创建合成网络或模型。"""

    def __init__(self) -> None:
        """显式单声道采样率使 SDK 继续运行同一音频调度链路。"""
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False), sample_rate=RATE, num_channels=1
        )

    def synthesize(self, text, **kwargs):
        """公开节点拥有固定音频，任何真实合成调用均为诊断实现错误。"""
        raise RuntimeError("固定音频只支持公开 tts_node")


async def fixed_audio(seconds: float):
    """稳定有声 PCM 避免把自然语音停顿误算成 SDK 停音，不生成或保存新 TTS 音频。"""
    count = round(seconds * RATE)
    samples = (np.sin(np.arange(count) * 2 * np.pi * 220 / RATE) * 1800).astype("<i2")
    for offset in range(0, count, FRAME_SAMPLES):
        frame = samples[offset : offset + FRAME_SAMPLES]
        yield rtc.AudioFrame(
            data=frame.tobytes(), sample_rate=RATE, num_channels=1, samples_per_channel=len(frame)
        )


class MeasuredAgent(Agent):
    """保持 SDK 轮次与音频调度真实，只通过公开节点切换重放与真实私有识别。"""

    def __init__(
        self, case: dict, source: RealtimeInput, events: list[dict], *, replay: bool
    ) -> None:
        """诊断不持有房间、DeepSeek 或 Jev；每个样本使用新的 SDK Session 隔离历史。"""
        super().__init__(instructions="固定声学诊断回复", use_tts_aligned_transcript=False)
        self.case = case
        self.source = source
        self.events = events
        self.replay = replay

    def record(self, kind: str, **values) -> None:
        """只记录本次合成样本相关的公开事件，不读取 SDK 的 activity、计时器或任务字段。"""
        self.events.append({"kind": kind, "mono": time.monotonic(), **values})

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings):
        """重放明确模拟识别事件和延迟；私有模式原样经过正式 LocalStreamingSTT 节点。"""
        if not self.replay:
            async for event in Agent.default.stt_node(self, audio, model_settings):
                if event.type in {
                    stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    stt.SpeechEventType.FINAL_TRANSCRIPT,
                }:
                    self.record(
                        "stt_transcript",
                        final=event.type == stt.SpeechEventType.FINAL_TRANSCRIPT,
                        text=event.alternatives[0].text if event.alternatives else "",
                    )
                yield event
            return
        elapsed = 0.0
        index = 0
        schedule = self.case["schedule"]
        async for frame in audio:
            elapsed += frame.duration
            while index < len(schedule) and schedule[index]["at"] <= elapsed:
                item = schedule[index]
                index += 1
                yield stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT
                    if item["final"]
                    else stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language="zh", text=item["text"], confidence=1)],
                    speech_end_time=(self.source.started_wall + item["end"])
                    if item["final"] and self.source.started_wall is not None
                    else None,
                )

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """实际轮次确认才计入提交次数，预生成文本或思考状态不作为抢话证据。"""
        self.record("turn_completed", text=new_message.text_content or "")

    async def llm_node(self, chat_ctx, tools, model_settings):
        """固定回复控制模型耗时变量，得到的是 SDK 声学策略延迟而非生产端到端延迟。"""
        await asyncio.sleep(0.005)
        yield "好的。"
        yield FlushSentinel()

    async def tts_node(self, text, model_settings):
        """完整消费 SDK 文本后提供固定有声片段，不调用本地或公共合成接口。"""
        async for _ in text:
            pass
        async for frame in fixed_audio(0.4):
            yield frame


def private_stt_configuration(path: Path) -> dict:
    """仅读取私有 STT 四项配置，不能依赖 LLM/TTS/Jev 密钥或默认公共推理地址。"""
    source = dotenv_values(path)
    values = {
        key: str(source.get(f"VOICE_AGENT_STT_{key.upper()}") or "").strip()
        for key in ("base_url", "model", "api_key")
    }
    values["language"] = "zh"
    try:
        url = urlsplit(values["base_url"])
        host = (url.hostname or "").rstrip(".")
        valid = (
            url.scheme in {"http", "https"}
            and bool(host)
            and (url.port is None or url.port > 0)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and host != "openai.com"
            and not host.endswith(".openai.com")
            and host != "livekit.cloud"
            and not host.endswith(".livekit.cloud")
        )
    except ValueError:
        valid = False
    if (
        not path.is_file()
        or not valid
        or values["model"] != "paraformer-streaming"
        or not values["api_key"]
    ):
        raise ValueError("需要 .env.local 的完整私有 Paraformer WS 配置")
    return values


def assess_trial(row: dict) -> dict:
    """完整句可正常接话，不完整问题提前提交或播放才是误触发；重放不能证明识别准确。"""
    events = row["events"]
    turn_events = [event for event in events if event["kind"] == "turn_completed"]
    starts = [event for event in events if event["kind"] == "audio_start"]
    pauses = [event for event in events if event["kind"] == "pause"]
    errors = [event for event in events if event["kind"] == "sdk_error"]
    speech_end = (
        row["input_started_mono"] + row["last_speech_seconds"]
        if row["last_speech_seconds"] is not None
        else None
    )
    incomplete = row["kind"] == "incomplete-pause"
    early_turns = [
        event
        for event in turn_events
        if speech_end is not None and event["mono"] < speech_end - 0.02
    ]
    early_audio = [
        event for event in starts if speech_end is not None and event["mono"] < speech_end - 0.02
    ]
    first = starts[0]["mono"] if starts else None
    recognized_text = "".join(event["text"] for event in turn_events)
    expected_text = row.get("expected_text", "")
    expected, recognized = normalized_text(expected_text), normalized_text(recognized_text)
    row.update(
        {
            "committed_turns": len(turn_events),
            "premature_turns": len(early_turns) if incomplete else 0,
            "premature_audio": len(early_audio) if incomplete else 0,
            "false_pauses": len(pauses) if row["kind"] == "noise-only" else 0,
            "false_turns": len(turn_events) if row["kind"] == "noise-only" else 0,
            "first_playout_after_input_ms": (first - speech_end) * 1000
            if first is not None and speech_end is not None and first >= speech_end
            else None,
            "pause_after_overlap_ms": (pauses[0]["mono"] - row["input_started_mono"] - 0.5) * 1000
            if pauses and row["kind"] == "acoustic-overlap"
            else None,
            "recognized_nonempty": any(event["text"].strip() for event in turn_events),
            "recognized_text": recognized_text,
            "recognition_exact_match": bool(expected) and expected == recognized,
            "recognition_cer": character_errors(expected, recognized) / len(expected)
            if expected
            else None,
            "sdk_errors": len(errors),
        }
    )
    return row


def normalized_text(text: str) -> str:
    """只去标点和空白并统一全角，禁止用语义猜测把识别错误改写成正确答案。"""
    return "".join(
        letter
        for letter in unicodedata.normalize("NFKC", text).lower()
        if not unicodedata.category(letter).startswith(("P", "Z")) and not letter.isspace()
    )


def character_errors(expected: str, recognized: str) -> int:
    """中文逐字符编辑距离单独报告，准确率不与声学提前回复或首帧延迟混为一项。"""
    previous = list(range(len(recognized) + 1))
    for index, letter in enumerate(expected, 1):
        current = [index]
        for position, target in enumerate(recognized, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[position] + 1,
                    previous[position - 1] + (letter != target),
                )
            )
        previous = current
    return previous[-1]


async def run_trial(
    case: dict, delay: float, duration: float, vad_model, *, config: dict | None
) -> dict:
    """每次独立 Session 经过真实 SDK；所有关闭有界，调用者取消仍撤销本样本输入和播放。"""
    events = []
    source = RealtimeInput()
    output = RealtimePlayback(events)
    agent = MeasuredAgent(case, source, events, replay=config is None)
    observed_vad = ObservedVAD(vad_model, events)
    recognizer = (
        ObservedRecognizer(events, **config, vad_model=observed_vad) if config else ReplaySTT()
    )
    language_model, voice = FixedLLM(), FixedTTS()
    detector = ObservedTurnDetector(events)
    session = AgentSession(
        stt=recognizer,
        vad=observed_vad,
        llm=language_model,
        tts=voice,
        turn_handling=public_turn_options(delay, duration, detector),
        tts_text_transforms=[],
        use_tts_aligned_transcript=False,
        user_away_timeout=None,
        aec_warmup_duration=AEC_WARMUP_SECONDS,
    )
    session.input.audio = source
    session.output.audio = output

    def observe_state(event) -> None:
        """公共状态仅用于辅助定位，不能取代 on_user_turn_completed 的真实提交边界。"""
        agent.record(event.type, state=event.new_state)

    def observe_error(event) -> None:
        """错误只保存类型，不把私有 WS 的响应、地址或鉴权写入验收文件。"""
        agent.record("sdk_error", error_type=type(event.error).__name__)

    session.on("user_state_changed", observe_state)
    session.on("agent_state_changed", observe_state)
    session.on("error", observe_error)
    row = {key: value for key, value in case.items() if key not in {"pcm", "schedule", "segments"}}
    row.update({"min_delay": delay, "min_duration": duration, "events": [], "error_type": None})
    row["aec_warmup_seconds"] = AEC_WARMUP_SECONDS
    row["early_output_interrupt_probe"] = case["kind"] in {"noise-only", "acoustic-overlap"}
    close_errors = []

    async def close_resource(resource) -> None:
        """单个关闭失败仍继续回收剩余拥有者，并把超时明确登记为本次验收失败。"""
        try:
            await asyncio.wait_for(resource.aclose(), 20)
        except Exception as error:
            close_errors.append(type(error).__name__)

    async with AsyncExitStack() as cleanup:
        for resource in (output, source, voice, language_model, recognizer, session):
            cleanup.push_async_callback(close_resource, resource)
        try:
            await asyncio.wait_for(session.start(agent, record=False), 15)
            if case["kind"] in {"noise-only", "acoustic-overlap"}:
                session.say(
                    "固定诊断播放",
                    audio=fixed_audio(case["duration_seconds"] + 4),
                    allow_interruptions=True,
                    add_to_chat_ctx=False,
                )
                async with asyncio.timeout(3):
                    while not any(event["kind"] == "audio_start" for event in events):
                        await asyncio.sleep(0.01)
            await asyncio.wait_for(source.feed(case["pcm"]), case["duration_seconds"] + 8)
            await asyncio.sleep(0.15)
        except Exception as error:
            row["error_type"] = type(error).__name__
        finally:
            # 资源收尾的 clear 不属于测试输入导致的误打断，事件在关闭前精确截断。
            row["events"] = list(events)
            row["input_started_mono"] = source.started_mono or time.monotonic()
    counts = Counter(event["kind"] for event in events)
    row["stream_resources"] = {
        kind: {
            "created": counts[f"{kind}_stream_created"],
            "closed": counts[f"{kind}_stream_closed"],
        }
        for kind in ("vad", "eot", "stt")
    }
    row["close_errors"] = close_errors
    row["actual_eot_predictions"] = sum(
        event["kind"] == "eot_prediction" and event["inference_duration"] is not None
        for event in row["events"]
    )
    row["resources_closed"] = (
        output.closed
        and output.worker.done()
        and source.closed
        and not close_errors
        and all(value["created"] == value["closed"] for value in row["stream_resources"].values())
    )
    return assess_trial(row)


def summarize(
    rows: list[dict], blockers: list[str], *, private_stt: bool, expected_counts: dict | None = None
) -> dict:
    """只有完整覆盖且全部质量门通过才能给出声学候选，永不声称浏览器首声最优。"""
    groups = []
    for delay, duration in GRID:
        selected = [
            row
            for row in rows
            if row["min_delay"] == delay
            and row["min_duration"] == duration
            and not row.get("warmup", False)
        ]
        delays = [
            row["first_playout_after_input_ms"]
            for row in selected
            if row["kind"] in {"latency", "noisy-latency"}
            and row["first_playout_after_input_ms"] is not None
            and row["error_type"] is None
        ]
        counts = Counter(row["kind"] for row in selected)
        failures = sum(
            bool(
                row["error_type"]
                or row["sdk_errors"]
                or not row["resources_closed"]
                or row["premature_turns"]
                or row["premature_audio"]
                or row["false_pauses"]
                or row["false_turns"]
                or (
                    row["kind"] in {"latency", "noisy-latency"}
                    and row["first_playout_after_input_ms"] is None
                )
                or (
                    row["kind"] in {"latency", "noisy-latency", "incomplete-pause"}
                    and row["committed_turns"] != 1
                )
                or (
                    row["kind"] in {"latency", "noisy-latency", "incomplete-pause"}
                    and row.get("actual_eot_predictions", 0) == 0
                )
            )
            for row in selected
        )
        positive_overlaps = [
            row
            for row in selected
            if row["kind"] == "acoustic-overlap" and row["overlap_seconds"] >= duration + 0.10
        ]
        missed = sum(row["pause_after_overlap_ms"] is None for row in positive_overlaps)
        coverage_complete = all(
            counts[kind] >= max(number, (expected_counts or {}).get(kind, 0))
            for kind, number in REQUIRED_COVERAGE.items()
        )
        quality_passed = (
            not blockers and coverage_complete and bool(delays) and failures == 0 and missed == 0
        )
        groups.append(
            {
                "min_delay": delay,
                "min_duration": duration,
                "trials": len(selected),
                "coverage": dict(counts),
                "coverage_complete": coverage_complete,
                "quality_failures": failures,
                "missed_acoustic_overlap": missed,
                "short_overlap_pauses": sum(
                    row["kind"] == "acoustic-overlap"
                    and row["overlap_seconds"] < duration
                    and row["pause_after_overlap_ms"] is not None
                    for row in selected
                ),
                "fixed_reply_playout_ms": distribution(delays),
                "quality_passed": quality_passed,
            }
        )
    passed = [group for group in groups if group["quality_passed"]]
    candidate = (
        min(passed, key=lambda group: group["fixed_reply_playout_ms"]["p95"]) if passed else None
    )
    recognition_rows = [
        row
        for row in rows
        if row["kind"] in {"latency", "noisy-latency", "incomplete-pause"}
        and not row.get("warmup", False)
    ]
    return {
        "mode": "private-stt-fixed-reply" if private_stt else "offline-stt-replay-fixed-reply",
        "real_models": ["CPU Silero", "local turn-detector-v1-mini"],
        "synthetic_input": True,
        "private_recognition_executed": private_stt,
        "private_recognition_verified": private_stt
        and bool(recognition_rows)
        and all(group["coverage_complete"] for group in groups)
        and all(
            row["recognition_exact_match"] and row["sdk_errors"] == 0 and row["error_type"] is None
            for row in recognition_rows
        ),
        "private_recognition_exact_matches": sum(
            row["recognition_exact_match"] for row in recognition_rows
        )
        if private_stt
        else None,
        "private_recognition_trials": len(recognition_rows) if private_stt else 0,
        "private_recognition_cer": distribution(
            [
                row["recognition_cer"]
                for row in recognition_rows
                if row["recognition_cer"] is not None
            ]
        )
        if private_stt
        else None,
        "browser_first_sound_verified": False,
        "production_optimum_selected": False,
        "clock": {
            "name": "monotonic",
            "implementation": time.get_clock_info("monotonic").implementation,
            "resolution_ms": time.get_clock_info("monotonic").resolution * 1000,
        },
        "blocked_categories": blockers,
        "groups": groups,
        "acoustic_candidate": candidate,
        "measurement": "真实 SDK + 声学模型 + 实时内存播放；固定回复非浏览器实际出声",
        "limits": [
            "离线重放不验证真实识别；私有模式不使用 DeepSeek、Jev 或真实 TTS。",
            "人工声学重叠不是完整中文打断指令；最终参数仍需真实识别与浏览器复核。",
            "内存播放二十毫秒帧和百毫秒缓冲，只用于 SDK 暂停/恢复及声学延迟。",
            "当前主机时钟分辨率单独报告；单重复小样本不能证明生产参数最优。",
            "识别逐句原文、去标点后的精确匹配与 CER 单独报告；声学候选不代表识别准确率通过。",
            "SDK 的 min_duration 依据 VAD speech_duration，包含 Silero 确认结束前的静音；"
            "短裁片暂停不是中文意图误触发证据。",
            "公开AEC预热显式0.0与当前生产一致，出声后立即输入噪声、约0.5秒注入重叠；未验证真实回声消除。",
        ],
    }


async def run_matrix(
    cases: list[dict],
    blockers: list[str],
    *,
    repeats: int,
    warmups: int,
    config: dict | None,
    output: Path,
) -> dict:
    """模型只在显式运行时初始化；报告拒绝覆盖，串行组合避免资源争抢或抹去失败证据。"""
    rows = []
    output.mkdir(parents=True, exist_ok=True)
    if any(
        (output / name).exists()
        for name in ("run-plan.json", "rows.jsonl", "progress.json", "summary.json")
    ):
        raise FileExistsError("输出目录已有诊断记录，请为本次运行选择新目录")
    started = time.monotonic()
    plan = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "mode": "private-stt-fixed-reply" if config else "offline-stt-replay-fixed-reply",
        "case_ids": [case["id"] for case in cases],
        "case_count": len(cases),
        "combinations": [list(pair) for pair in GRID],
        "repeats": repeats,
        "formal_trials": len(cases) * repeats * len(GRID),
        "warmup_trials": warmups * len(GRID),
        "blocked_categories": blockers,
        "synthetic_audio": True,
        "browser_first_sound_verified": False,
        "clock_resolution_ms": time.get_clock_info("monotonic").resolution * 1000,
    }
    with (output / "run-plan.json").open("x", encoding="utf-8") as writer:
        writer.write(json.dumps(plan, ensure_ascii=False, indent=2))

    def progress(status: str, error_type: str | None = None) -> None:
        """每轮资源关闭后原子保存进度，异常与取消仍可看清已完成范围。"""
        state = {
            "status": status,
            "formal_completed": sum(not row["warmup"] for row in rows),
            "formal_expected": plan["formal_trials"],
            "warmups_completed": sum(row["warmup"] for row in rows),
            "elapsed_seconds": time.monotonic() - started,
            "last_case": rows[-1]["id"] if rows else None,
            "error_type": error_type,
        }
        temporary = output / "progress.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output / "progress.json")

    progress("initializing")
    try:
        await asyncio.to_thread(init_eot)
        vad_model = await asyncio.to_thread(
            silero.VAD.load, min_silence_duration=0.30, force_cpu=True
        )
        progress("running")
        with (output / "rows.jsonl").open("w", encoding="utf-8") as writer:
            for repeat in range(-warmups, repeats):
                chosen = (
                    [case for case in cases if case["kind"] == "latency"][:1]
                    if repeat < 0
                    else cases
                )
                order = GRID[repeat % len(GRID) :] + GRID[: repeat % len(GRID)]
                for case in chosen:
                    for delay, duration in order:
                        row = await run_trial(case, delay, duration, vad_model, config=config)
                        row.update({"repeat": repeat, "warmup": repeat < 0})
                        rows.append(row)
                        writer.write(json.dumps(row, ensure_ascii=False) + "\n")
                        writer.flush()
                        progress("running")
                        print(
                            json.dumps(
                                {
                                    "case": case["id"],
                                    "repeat": repeat,
                                    "min_delay": delay,
                                    "min_duration": duration,
                                    "error_type": row["error_type"],
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
    except BaseException as error:
        progress("interrupted", type(error).__name__)
        raise
    # Silero 模型无 aclose；权重进程内复用，真实流在每个 Session 关闭时由公开接口回收。
    summary = summarize(
        rows,
        blockers,
        private_stt=config is not None,
        expected_counts={
            kind: count * repeats for kind, count in Counter(case["kind"] for case in cases).items()
        },
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    progress("completed")
    return summary


def main(argv: list[str] | None = None) -> int:
    """默认只列计划不加载模型，实际运行须显式选择识别边界；报告只写被忽略目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline-replay", action="store_true", help="真实声学模型，重放 STT 与固定回复"
    )
    mode.add_argument(
        "--private-stt", action="store_true", help="真实声学模型与私有 WS 识别，固定回复"
    )
    parser.add_argument(
        "--plan", action="store_true", help="只检查素材、覆盖和运行预算，不加载推理模型"
    )
    parser.add_argument("--materials", type=Path, default=MATERIALS)
    parser.add_argument("--questions-manifest", type=Path, default=QUESTIONS)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    args.plan = args.plan or not (args.offline_replay or args.private_stt)
    if args.repeats < 1 or args.warmups < 0:
        parser.error("repeats 必须大于零，warmups 不能为负")
    try:
        cases, blockers = read_cases(args.materials, args.questions_manifest)
        plan = {
            "cases": len(cases),
            "coverage": dict(Counter(case["kind"] for case in cases)),
            "combinations": [
                {"min_delay": d, "min_duration": v, "max_delay": 1.10} for d, v in GRID
            ],
            "blocked_categories": blockers,
            "expected_minutes": round(
                sum(case["duration_seconds"] + 3.0 for case in cases) * args.repeats * 6 / 60, 1
            ),
            "required_questions": [
                {
                    "id": i,
                    "prefix_text": p,
                    "suffix_text": s,
                    "prefix_file": f"{i}-prefix.wav",
                    "suffix_file": f"{i}-suffix.wav",
                    "prefix_sha256": "填写实际文件摘要",
                    "suffix_sha256": "填写实际文件摘要",
                    "trimmed_and_reviewed": False,
                }
                for i, p, s in QUESTION_SPEC
            ],
            "question_manifest_format": {
                "synthetic_audio": True,
                "contains_user_audio": False,
                "questions": "required_questions 三条",
            },
        }
        if args.plan:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = (args.output or PROJECT_ROOT / ".tools" / "turn-benchmark" / timestamp).resolve()
        if not output.is_relative_to((PROJECT_ROOT / ".tools").resolve()):
            raise ValueError("报告必须位于项目被忽略的 .tools 目录")
        config = private_stt_configuration(args.env_file) if args.private_stt else None
        summary = asyncio.run(
            run_matrix(
                cases,
                blockers,
                repeats=args.repeats,
                warmups=args.warmups,
                config=config,
                output=output,
            )
        )
        print(
            json.dumps({"report_directory": str(output), **summary}, ensure_ascii=False, indent=2)
        )
        return 0 if summary["acoustic_candidate"] is not None else 1
    except Exception as error:
        print(f"轮次诊断失败（{type(error).__name__}）；检查素材、标注与所选私有 STT 配置。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
