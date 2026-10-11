"""在真实合成节点测量 Jev 的并行边界；默认离线，首帧不等同于浏览器实际出声。"""

import argparse
import asyncio
import json
import math
import sys
import time
from collections import Counter
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values
from livekit import rtc
from livekit.agents import APIConnectOptions
from livekit.plugins import openai

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent, SpeechSegment
from xiaoya.domain.presentation import PresentationContext, PresentationDecision
from xiaoya.infrastructure.expressive_agent import VOICE_INSTRUCTIONS, ExpressiveAgent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEXT = "你好呀，我们慢慢聊。"
MEASUREMENT = "服务端 ExpressiveAgent._synthesize 首个 AudioFrame，非浏览器实际出声"


def percentile(values: list[float], percentage: float) -> float | None:
    """使用线性插值并保留无样本的空值，避免少量数据或失败请求制造零延迟。"""
    if not 0 <= percentage <= 100 or any(not math.isfinite(value) for value in values):
        raise ValueError("分位数及样本必须是合法有限数值")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(values: list[float]) -> dict[str, int | float | None]:
    """明确样本数，使空值、暖机与正式统计不能混作已经通过的配对验收。"""
    return {"count": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def resources_balanced(counts: Counter) -> bool:
    """同时核对资源拥有者和实际音频流，进程退出不能代替显式关闭的证据。"""
    return (
        all(
            counts[f"{name}_created"] == counts[f"{name}_closed"]
            for name in ("tts_clients", "tts_streams", "jev_ports", "controllers", "agents")
        )
        and counts["unfinished_workers"] == 0
    )


class MockJev:
    """固定半秒的异步判断模拟慢增强服务，不连接官方 API 或读取真实 Key。"""

    def __init__(self, counts: Counter, *, timeout: bool = False) -> None:
        """超时探针仅切换返回结果，延迟与正常模型保持一致，便于证明语音不等待。"""
        self.counts = counts
        self.timeout = timeout
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.closed = False
        counts["jev_ports_created"] += 1

    async def decide(self, context: PresentationContext) -> PresentationDecision:
        """正常返回有效有限决策，取消与超时分别计数，不能把取消伪装成模型成功。"""
        self.counts["jev_requests_started"] += 1
        self.started.set()
        try:
            await asyncio.sleep(0.5)
            if self.timeout:
                self.counts["jev_requests_timed_out"] += 1
                raise TimeoutError("模拟 Jev 超时")
            self.counts["jev_requests_returned"] += 1
            return PresentationDecision(DeliveryIntent("happy", "nod"), 0.99, 0.99, 0.99, 0.6)
        except asyncio.CancelledError:
            self.counts["jev_requests_cancelled"] += 1
            raise
        finally:
            self.finished.set()

    async def aclose(self) -> None:
        """端口只统计自己的首次关闭，实际任务由正式控制器撤销。"""
        if not self.closed:
            self.closed = True
            self.counts["jev_ports_closed"] += 1


class MockAudioStream:
    """三帧实际 SDK 音频对象避免用字符串替代首个 AudioFrame 验收。"""

    async def __aenter__(self):
        """模拟公开合成流的异步资源上下文，不启动设备或网络。"""
        return self

    async def __aexit__(self, *_args) -> None:
        """资源计数由包装器统一记录，异常与正常完成共用同一关闭边界。"""

    async def __aiter__(self):
        """首帧等待让真正的表现 worker 并行启动，后续帧必须全部读取后结束。"""
        for index in range(3):
            await asyncio.sleep(0.025 if index == 0 else 0.010)
            frame = rtc.AudioFrame.create(
                sample_rate=24000, num_channels=1, samples_per_channel=240
            )
            yield SimpleNamespace(frame=frame)


class MockTTS:
    """默认路径没有任何外部客户端，保持与私有插件相同的公开调用边界。"""

    def synthesize(self, text: str, *, conn_options: APIConnectOptions):
        """只提供流，不在调用时阻塞；正式节点仍负责文本过滤和固定声音采样。"""
        return MockAudioStream()

    async def aclose(self) -> None:
        """模拟客户端的明确拥有权，禁止依赖垃圾回收完成验收。"""


class CountedStream:
    """包装公开流接口收集回收证据，不改变音频读取顺序或增加取消。"""

    def __init__(self, stream: Any, counts: Counter) -> None:
        """创建即登记流拥有权，开流失败也必须经过真实上下文清理。"""
        self.stream = stream
        self.counts = counts
        self.entered = None
        counts["tts_streams_created"] += 1

    async def __aenter__(self):
        """由 SDK 流自行建立请求，包装器不提前发起额外 HTTP。"""
        try:
            self.entered = await self.stream.__aenter__()
        except BaseException:
            await self.stream.__aexit__(*sys.exc_info())
            self.counts["tts_streams_closed"] += 1
            raise
        return self

    async def __aexit__(self, *args) -> None:
        """完整读取或失败后仍关闭原流，计数只在真实关闭成功后更新。"""
        await self.stream.__aexit__(*args)
        self.counts["tts_streams_closed"] += 1

    async def __aiter__(self):
        """逐帧透传唯一合成流，仅记录数量，不缓存音频或另建可听出口。"""
        async for event in self.entered:
            self.counts["audio_frames_consumed"] += 1
            yield event


class CountedTTS:
    """每个私有插件客户端全程复用，交替顺序和暖机消除首次连接的单边偏差。"""

    def __init__(self, backend: Any, counts: Counter) -> None:
        """客户端创建成功才计入拥有清单，后续失败由外层 ExitStack 逐项回收。"""
        self.backend = backend
        self.counts = counts
        counts["tts_clients_created"] += 1

    def synthesize(self, text: str, *, conn_options: APIConnectOptions):
        """调用已有接口并包装原流，max_retry 仍由真实合成节点决定。"""
        return CountedStream(self.backend.synthesize(text, conn_options=conn_options), self.counts)

    async def aclose(self) -> None:
        """公开插件 aclose 回收它拥有的 HTTP 客户端，不能仅关闭音频迭代器。"""
        await self.backend.aclose()
        self.counts["tts_clients_closed"] += 1


class BenchmarkAgent(ExpressiveAgent):
    """运行正式合成函数，Session 只提供 SDK 公开的连接选项与当前 Speech 属性。"""

    def __init__(self, voices: dict, presentation: PresentationController) -> None:
        """未创建真实房间或 AgentSession，避免诊断首帧时隐式启动其他模型服务。"""
        super().__init__(
            instructions="独立首帧诊断",
            tools=[],
            room=SimpleNamespace(),
            voices=voices,
            presentation=presentation,
        )
        self.benchmark_session = SimpleNamespace(
            conn_options=SimpleNamespace(tts_conn_options=APIConnectOptions()), current_speech=None
        )

    @property
    def session(self):
        """覆写公开属性满足正式节点依赖，不访问 SDK activity 或生成任务私有字段。"""
        return self.benchmark_session


async def close_owned(resource: Any, counts: Counter, kind: str) -> None:
    """收敛诊断对象的回收计数，不替代它们自身的取消、网络或输出清理。"""
    await resource.aclose()
    counts[f"{kind}_closed"] += 1
    if kind == "controllers" and resource._worker is not None and not resource._worker.done():
        counts["unfinished_workers"] += 1


async def measure_segment(
    voices: dict,
    counts: Counter,
    *,
    enabled: bool,
    pair: int,
    warmup: bool = False,
    timeout: bool = False,
    wait_for_decision: bool = False,
) -> dict:
    """计时仅覆盖准备至首帧，完整消费后关闭；额外探针等待不计入音频延迟。"""
    decider = MockJev(counts, timeout=timeout) if enabled else None
    controller = PresentationController(decider)
    agent = BenchmarkAgent(voices, controller)
    counts["controllers_created"] += 1
    counts["agents_created"] += 1
    row = {
        "pair": pair,
        "mode": "mock-jev" if enabled else "without-jev",
        "warmup": warmup,
        "first_audio_frame_ms": None,
        "full_audio_ms": None,
        "frames": 0,
        "audio_duration_ms": 0.0,
        "audio_complete": False,
        "jev_started_at_first_frame": False,
        "jev_finished_at_first_frame": False,
        "error_type": None,
    }
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(close_owned, controller, counts, "controllers")
        cleanup.push_async_callback(close_owned, agent, counts, "agents")
        started = time.perf_counter()
        controller.complete_turn("我们聊两句吧。")
        try:
            async for frame in agent._synthesize(SpeechSegment(TEXT)):
                if not isinstance(frame, rtc.AudioFrame):
                    raise TypeError("合成结果不是 AudioFrame")
                if row["first_audio_frame_ms"] is None:
                    row["first_audio_frame_ms"] = (time.perf_counter() - started) * 1000
                    if decider is not None:
                        row["jev_started_at_first_frame"] = decider.started.is_set()
                        row["jev_finished_at_first_frame"] = decider.finished.is_set()
                row["frames"] += 1
                row["audio_duration_ms"] += frame.duration * 1000
            row["full_audio_ms"] = (time.perf_counter() - started) * 1000
            row["audio_complete"] = row["frames"] > 0
            if wait_for_decision and decider is not None:
                await asyncio.wait_for(decider.finished.wait(), 1.1)
        except Exception as error:
            # 错误只记录类型；供应商正文可能含鉴权或内部地址，不写入报告。
            row["error_type"] = type(error).__name__
    row["worker_closed"] = controller._worker is None or controller._worker.done()
    return row


def private_tts_configuration(path: Path) -> dict[str, str]:
    """只读取显式文件中的 TTS 配置，诊断不能继承公共默认地址或依赖 LLM/Jev 密钥。"""
    if not path.is_file():
        raise ValueError("私有 TTS 验收需要已有 .env.local 文件")
    source = dotenv_values(path)
    values = {
        key: str(source.get(f"VOICE_AGENT_TTS_{key.upper()}") or "").strip()
        for key in ("base_url", "model", "voice", "api_key")
    }
    values["response_format"] = str(source.get("VOICE_AGENT_TTS_RESPONSE_FORMAT") or "wav").strip()
    if any(not value for value in values.values()):
        raise ValueError("请完整填写 .env.local 中私有 TTS 的地址、模型、音色和独立密钥")
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
        not valid
        or values["model"] != "cosyvoice3-0.5b"
        or values["voice"] != "default"
        or values["response_format"] not in {"wav", "pcm"}
    ):
        raise ValueError("私有 TTS 配置必须符合现行 CosyVoice3 地址、模型、音色和音频格式契约")
    return values


def make_voices(cleanup: AsyncExitStack, counts: Counter, config: dict | None) -> dict:
    """三种声音与正式装配一致；共用客户端跨样本复用，每个成功构造立即登记关闭。"""
    voices = {}
    for preset, instructions in {"neutral": None, **VOICE_INSTRUCTIONS}.items():
        kwargs = dict(config or {})
        if instructions is not None:
            kwargs["instructions"] = instructions
        voice = CountedTTS(openai.TTS(**kwargs) if config is not None else MockTTS(), counts)
        cleanup.push_async_callback(voice.aclose)
        voices[preset] = voice
    return voices


def summarize(rows: list[dict], counts: Counter, pairs: int) -> dict:
    """验收比较两模式 p95，逐对差值只描述波动；两者不是可互换的分位数。"""
    measured = [row for row in rows if not row["warmup"]]
    valid = [
        row
        for row in measured
        if row["audio_complete"]
        and row["first_audio_frame_ms"] is not None
        and not row["error_type"]
    ]
    modes = {
        mode: latency_summary([row["first_audio_frame_ms"] for row in valid if row["mode"] == mode])
        for mode in ("without-jev", "mock-jev")
    }
    paired = {}
    for row in valid:
        paired.setdefault(row["pair"], {})[row["mode"]] = row["first_audio_frame_ms"]
    increases = [
        values["mock-jev"] - values["without-jev"] for values in paired.values() if len(values) == 2
    ]
    increase_summary = latency_summary(increases)
    p95_increase = (
        modes["mock-jev"]["p95"] - modes["without-jev"]["p95"]
        if modes["mock-jev"]["p95"] is not None and modes["without-jev"]["p95"] is not None
        else None
    )
    return {
        "measurement": MEASUREMENT,
        "expected_pairs": pairs,
        "valid_pairs": len(increases),
        "first_audio_frame_ms": modes,
        "paired_increase_ms": increase_summary,
        "paired_jitter_within_100ms": (
            increase_summary["p95"] <= 100 if increase_summary["p95"] is not None else None
        ),
        "group_p95_increase_ms": p95_increase,
        "latency_gate_definition": "p95(mock-jev) - p95(without-jev) <= 100ms",
        "resources": dict(counts),
        "resources_balanced": resources_balanced(counts),
        "gate_passed": (
            len(increases) == pairs
            and resources_balanced(counts)
            and p95_increase is not None
            and p95_increase <= 100
            and all(row["worker_closed"] for row in rows)
        ),
    }


async def run_benchmark(pairs: int, warmups: int, config: dict | None = None) -> tuple[dict, list]:
    """交替两种模式降低 GPU 热状态偏差，暖机保留原始记录但不进入正式分位数。"""
    counts = Counter()
    rows = []
    async with AsyncExitStack() as cleanup:
        voices = make_voices(cleanup, counts, config)
        for index in range(warmups + pairs):
            warmup = index < warmups
            pair = index - warmups
            for enabled in (False, True) if index % 2 == 0 else (True, False):
                rows.append(
                    await measure_segment(voices, counts, enabled=enabled, pair=pair, warmup=warmup)
                )
    summary = summarize(rows, counts, pairs)
    # 两个额外探针始终使用离线音频，私有模式也不会为等待装饰结果占用 GPU。
    probe_counts = Counter()
    probes = {}
    async with AsyncExitStack() as cleanup:
        voices = make_voices(cleanup, probe_counts, None)
        for name, timeout in (("delayed_valid_decision", False), ("timeout", True)):
            probes[name] = await measure_segment(
                voices,
                probe_counts,
                enabled=True,
                pair=-1,
                timeout=timeout,
                wait_for_decision=True,
            )
    probe_passed = (
        resources_balanced(probe_counts)
        and probe_counts["jev_requests_returned"] == 1
        and probe_counts["jev_requests_timed_out"] == 1
        and all(
            row["audio_complete"]
            and row["jev_started_at_first_frame"]
            and not row["jev_finished_at_first_frame"]
            and not row["error_type"]
            for row in probes.values()
        )
    )
    summary.update(
        {
            "mode": "private-tts-with-mock-jev" if config else "offline-mock-tts-and-jev",
            "mock_jev_delay_ms": 500,
            "warmup_pairs": warmups,
            "percentile_method": "linear interpolation (n-1)",
            "probes": probes,
            "probe_resources": dict(probe_counts),
            "probe_passed": probe_passed,
            "gate_passed": summary["gate_passed"] and probe_passed,
            "limits": [
                "不测试浏览器播放、真人设备、识别、DeepSeek、真实 Jev 或完整房间链路。",
                "私有模式只调用 .env.local 中已配置的私有 TTS。",
                "音频完整消费后才结束本样本；结束时取消尚未完成的 Mock Jev。",
            ],
        }
    )
    return summary, rows


def main(argv: list[str] | None = None) -> int:
    """联网仅由显式参数授权；报告固定落在被忽略目录，不输出配置密钥或音频。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-tts", action="store_true", help="只调用显式配置的私有 TTS")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--pairs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5, help="暖机配对数，默认每模式各 5 次")
    parser.add_argument("--output", type=Path, help="报告目录，必须位于项目被忽略的 .tools 内")
    args = parser.parse_args(argv)
    if args.pairs < 1 or args.warmups < 0:
        parser.error("pairs 必须大于零，warmups 不能为负")
    try:
        config = private_tts_configuration(args.env_file) if args.private_tts else None
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = args.output or PROJECT_ROOT / ".tools" / "presentation-benchmark" / timestamp
        output = output.resolve()
        if not output.is_relative_to((PROJECT_ROOT / ".tools").resolve()):
            raise ValueError("报告目录必须位于项目被忽略的 .tools 内")
        summary, rows = asyncio.run(run_benchmark(args.pairs, args.warmups, config))
        output.mkdir(parents=True, exist_ok=True)
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output / "rows.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        print(
            json.dumps({"report_directory": str(output), **summary}, ensure_ascii=False, indent=2)
        )
        return 0 if summary["gate_passed"] else 1
    except Exception as error:
        print(f"首帧诊断失败（{type(error).__name__}）；请核对配置与服务。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
