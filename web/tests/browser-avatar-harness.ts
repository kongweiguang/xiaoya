import { RemoteAudioTrack } from 'livekit-client';
import {
  AvatarAudioBridge,
  type SessionAudio,
  createSessionAudio,
} from '../lib/avatar/audio-bridge';
import { LipSyncTimeline, getAudibleAudioTime, rmsAmplitude } from '../lib/avatar/lip-sync';
import { type AvatarFrame, Live2DRuntime } from '../lib/avatar/live2d-runtime';
import { type MotionSyncAnalyzer, createMotionSyncAnalyzer } from '../lib/avatar/motion-sync';
import { loadSdkScript } from '../lib/avatar/sdk-loader';

type Utterance = {
  id: string;
  file: string;
  type: string;
  sha256: string;
  inserted_silences: { start_seconds: number; duration_ms: number }[];
};
type Manifest = { complete: boolean; utterances: Utterance[]; synthetic_audio: boolean };
type RenderSample = { audioAt: number; wallAt: number; open: number; form: number };
type Trial = { utterance: Utterance; startAt: number; samples: RenderSample[] };
type OwnedSession = {
  audio: SessionAudio;
  destination: MediaStreamAudioDestinationNode;
  track: RemoteAudioTrack;
  bridge: AvatarAudioBridge;
  analyzer: MotionSyncAnalyzer;
  liveNodes: Set<AudioNode>;
};
type TrialResult = {
  id: string;
  type: string;
  expectedStartAt: number;
  expectedEndAt: number;
  actualOpenAt: number | null;
  actualCloseAt: number | null;
  openingOffsetMs: number | null;
  tailClosingOffsetMs: number | null;
  pauseChecks: { startAt: number; frames: number; allClosed: boolean }[];
  trace: RenderSample[];
};
type Telemetry = {
  at: number;
  fps: number;
  timelineSize: number;
  queuedSamples: number;
  contextState: string;
  hidden: boolean;
  heapBytes: number | null;
};
type ResourceSnapshot = {
  cycle: number;
  liveNodesAfterDispose: number;
  trackListenersAfterDispose: number;
  queueAfterDispose: number;
  originalTrackLiveBeforeHarnessStop: boolean;
  contextClosed: boolean;
};

const canvas = document.querySelector<HTMLCanvasElement>('#avatar')!;
const status = document.querySelector<HTMLElement>('#status')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const runButton = document.querySelector<HTMLButtonElement>('#run')!;
const stabilityButton = document.querySelector<HTMLButtonElement>('#stability')!;
const saveButton = document.querySelector<HTMLButtonElement>('#save')!;
const cancelButton = document.querySelector<HTMLButtonElement>('#cancel')!;
const timeline = new LipSyncTimeline();
const modes = new Set<string>();
const failures: string[] = [];
const buffers = new Map<string, AudioBuffer>();
const trials: TrialResult[] = [];
const telemetry: Telemetry[] = [];
const resources: ResourceSnapshot[] = [];
const createdAt = new Date().toISOString();
let runtime: Live2DRuntime;
let manifest: Manifest;
let current: OwnedSession | null = null;
let activeTrial: Trial | null = null;
let controller: AbortController | null = null;
let frameCount = 0;
let fpsFrames = 0;
let fpsAt = performance.now();
let hiddenFrames = 0;
let maxTimeline = 0;
let maxPcmQueue = 0;
let contextsCreated = 0;
let contextsClosed = 0;
let cycleCount = 0;
let stability: {
  startedAt: string;
  elapsedSeconds: number;
  iterations: number;
  complete: boolean;
} | null = null;

/** 只展示摘要，详细逐帧证据写到服务器文件，避免长测试把 DOM 和 React 状态撑大。 */
function updateStatus(message: string) {
  status.textContent = message;
  document.body.dataset.state = controller ? 'running' : 'ready';
}

/** 有界时间线与原生 PCM 队列按秒抽样，30 分钟测试不会无限保存逐帧记录。 */
function onFrame(frame: AvatarFrame) {
  frameCount++;
  fpsFrames++;
  if (document.hidden) hiddenFrames++;
  const now = performance.now();
  const audioAt = current ? getAudibleAudioTime(current.audio.context, now) : 0;
  if (activeTrial && activeTrial.samples.length < 5_000)
    activeTrial.samples.push({ audioAt, wallAt: now / 1000, open: frame.open, form: frame.form });
  maxTimeline = Math.max(maxTimeline, timeline.size);
  maxPcmQueue = Math.max(maxPcmQueue, current?.analyzer.queuedSamples ?? 0);
  if (now - fpsAt >= 1_000) {
    telemetry.push({
      at: now / 1000,
      fps: (fpsFrames * 1000) / (now - fpsAt),
      timelineSize: timeline.size,
      queuedSamples: current?.analyzer.queuedSamples ?? 0,
      contextState: current?.audio.context.state ?? 'none',
      hidden: document.hidden,
      heapBytes:
        (performance as Performance & { memory?: { usedJSHeapSize: number } }).memory
          ?.usedJSHeapSize ?? null,
    });
    if (telemetry.length > 2_048) telemetry.shift();
    fpsFrames = 0;
    fpsAt = now;
  }
}

/** 短等待持续检查取消，测试超时或手动结束不会留下仍在播放的长音频。 */
async function waitUntilAudio(at: number, signal: AbortSignal) {
  const deadline = performance.now() + 60_000;
  while (current && getAudibleAudioTime(current.audio.context) < at) {
    signal.throwIfAborted();
    if (performance.now() > deadline) throw new Error('音频时钟未推进，请保持测试页可见且允许声音');
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
  }
}

/** 仅在测试页包装公开节点释放方法，记录实际断图；不读取 SDK 的内部附件字段。 */
function instrumentNodes(context: AudioContext): Set<AudioNode> {
  const live = new Set<AudioNode>();
  /** 一次完整断开即结束节点的测试所有权，重复释放不造成负数计数。 */
  function own<T extends AudioNode>(node: T): T {
    live.add(node);
    const disconnect = node.disconnect.bind(node);
    /** 当前桥接只使用完整断开；保持同样语义并记录释放而非仅清空 JS 引用。 */
    node.disconnect = () => {
      disconnect();
      live.delete(node);
    };
    return node;
  }
  const source = context.createMediaStreamSource.bind(context);
  const gain = context.createGain.bind(context);
  context.createMediaStreamSource = (stream) => own(source(stream));
  context.createGain = () => own(gain());
  return live;
}

/** 每次连接拥有独立时钟和合成轨；分析只借用轨道，唯一可听出口来自 BufferSource。 */
async function connectSession(signal: AbortSignal): Promise<OwnedSession> {
  const audio = createSessionAudio();
  contextsCreated++;
  const liveNodes = instrumentNodes(audio.context);
  try {
    await audio.context.resume();
    if (audio.context.state !== 'running') throw new Error('浏览器未允许播放测试声音');
    signal.throwIfAborted();
    if (!runtime.motionSyncBuffer) throw new Error('正式模型缺少 MotionSync 配置');
    await loadSdkScript('/avatar/vendor/live2dcubismmotionsynccore.min.js');
    const analyzer = await createMotionSyncAnalyzer(runtime.model, runtime.motionSyncBuffer, {
      /** 降级是可见的验收失败，不能把幅度分析当作官方 MotionSync 成功。 */
      onModeChange(mode, error) {
        modes.add(mode);
        if (error) failures.push(error.message);
      },
    });
    modes.add(analyzer.mode);
    if (signal.aborted) {
      analyzer.dispose();
      signal.throwIfAborted();
    }
    const destination = audio.context.createMediaStreamDestination();
    const mediaTrack = destination.stream.getAudioTracks()[0];
    const track = new RemoteAudioTrack(
      mediaTrack,
      'synthetic-test',
      {} as RTCRtpReceiver,
      audio.context
    );
    const bridge = new AvatarAudioBridge({
      context: audio.context,
      /** 主线程只保存参数，不把 PCM 与用户录音写入长期证据。 */
      onSamples(samples, rate, at) {
        if (!signal.aborted)
          for (const frame of analyzer.sample(samples, rate, at)) timeline.push(frame);
      },
      /** 静音与换轨立即清除保持帧，时钟更换时不复用前一连接的口型。 */
      onSilent() {
        timeline.clear();
        analyzer.reset();
      },
      /** 运行期故障进入报告，测试仍保证后续回收资源。 */
      onError(error) {
        failures.push(error.message);
      },
    });
    const session = { audio, destination, track, bridge, analyzer, liveNodes };
    current = session;
    bridge.setPlaybackAvailable(true);
    try {
      await bridge.attach(track, signal);
      return session;
    } catch (error) {
      await disconnectSession();
      throw error;
    }
  } catch (error) {
    if (audio.context.state !== 'closed') {
      await audio.close();
      contextsClosed++;
    }
    throw error;
  }
}

/** 先释放借用节点再停测试自己拥有的合成轨，显式证明 bridge 不会停止 SDK 原轨。 */
async function disconnectSession() {
  const session = current;
  current = null;
  if (!session) return;
  session.bridge.dispose();
  const trackStillLive = session.track.mediaStreamTrack.readyState === 'live';
  const listeners = session.track.eventNames().length;
  session.analyzer.dispose();
  const queued = session.analyzer.queuedSamples;
  session.destination.disconnect();
  session.destination.stream.getTracks().forEach((track) => track.stop());
  await session.audio.close();
  contextsClosed++;
  timeline.clear();
  resources.push({
    cycle: cycleCount,
    liveNodesAfterDispose: session.liveNodes.size,
    trackListenersAfterDispose: listeners,
    queueAfterDispose: queued,
    originalTrackLiveBeforeHarnessStop: trackStillLive,
    contextClosed: session.audio.context.state === 'closed',
  });
  if (resources.length > 64) resources.shift();
  if (session.liveNodes.size || listeners || queued || !trackStillLive)
    failures.push('分析资源释放或轨道所有权验证失败');
}

/** 解码只保留 30 条固定合成输入，文件哈希先确认素材一致，避免更换样本后沿用旧结论。 */
async function decode(utterance: Utterance): Promise<AudioBuffer> {
  const cached = buffers.get(utterance.id);
  if (cached) return cached;
  if (!current) throw new Error('测试音频上下文未创建');
  const response = await fetch('/utterances/' + encodeURIComponent(utterance.file));
  if (!response.ok) throw new Error('合成样本加载失败');
  const content = await response.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', content);
  const checksum = Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, '0')
  ).join('');
  if (checksum !== utterance.sha256) throw new Error('合成样本哈希不一致');
  const buffer = await current.audio.context.decodeAudioData(content);
  buffers.set(utterance.id, buffer);
  return buffer;
}

/** 20 ms 窗口沿实际解码后的采样率测量，首音用窗口中心、尾音用最后活动窗口末端。 */
function expectedSpeech(buffer: AudioBuffer) {
  const pcm = buffer.getChannelData(0);
  const count = Math.round(buffer.sampleRate * 0.02);
  let first = -1;
  let last = -1;
  for (let offset = 0; offset < pcm.length; offset += count) {
    if (rmsAmplitude(pcm.subarray(offset, Math.min(offset + count, pcm.length))) >= 0.012) {
      if (first < 0) first = offset;
      last = offset;
    }
  }
  if (first < 0) throw new Error('合成样本没有超过 0.012 RMS 的活动窗口');
  return {
    first: (first + count / 2) / buffer.sampleRate,
    end: Math.min(last + count, pcm.length) / buffer.sampleRate,
  };
}

/** 只建立一路扬声器播放；第二支路送测试轨和录像，分析节点自身保持零增益。 */
async function play(utterance: Utterance, signal: AbortSignal, measure = true) {
  const buffer = await decode(utterance);
  signal.throwIfAborted();
  const session = current!;
  const source = session.audio.context.createBufferSource();
  source.buffer = buffer;
  source.connect(session.audio.context.destination);
  source.connect(session.destination);
  const startAt = session.audio.context.currentTime + 0.12;
  const energy = expectedSpeech(buffer);
  const trial: Trial = { utterance, startAt, samples: [] };
  activeTrial = measure ? trial : null;
  timeline.clear();
  session.analyzer.reset();
  runtime.setBehavior('speaking');
  /** 取消只停止测试自己生成的 BufferSource，生产的借用轨道生命周期不受影响。 */
  function stop() {
    source.stop();
  }
  signal.addEventListener('abort', stop, { once: true });
  try {
    source.start(startAt);
    await waitUntilAudio(startAt + buffer.duration + 0.18, signal);
    if (measure) {
      const expectedStartAt = startAt + energy.first;
      const expectedEndAt = startAt + energy.end;
      const firstOpen = trial.samples.find(
        (sample) => sample.audioAt >= startAt && sample.open >= 0.025
      );
      const lastOpen = trial.samples.findLast(
        (sample) => sample.audioAt >= startAt && sample.open > 0.02
      );
      const closed = lastOpen
        ? trial.samples.find((sample) => sample.audioAt > lastOpen.audioAt && sample.open <= 0.02)
        : undefined;
      const pauseChecks = utterance.inserted_silences.map((pause) => {
        const at = startAt + pause.start_seconds;
        const frames = trial.samples.filter(
          (sample) =>
            sample.audioAt >= at + 0.15 && sample.audioAt <= at + pause.duration_ms / 1000 - 0.02
        );
        return {
          startAt: at,
          frames: frames.length,
          allClosed: frames.length > 0 && frames.every((sample) => sample.open <= 0.02),
        };
      });
      trials.push({
        id: utterance.id,
        type: utterance.type,
        expectedStartAt,
        expectedEndAt,
        actualOpenAt: firstOpen?.audioAt ?? null,
        actualCloseAt: closed?.audioAt ?? null,
        openingOffsetMs: firstOpen ? (firstOpen.audioAt - expectedStartAt) * 1000 : null,
        tailClosingOffsetMs: closed ? (closed.audioAt - expectedEndAt) * 1000 : null,
        pauseChecks,
        trace: trial.samples,
      });
      summary.textContent = JSON.stringify(report(false).summary, null, 2);
    }
  } finally {
    signal.removeEventListener('abort', stop);
    source.disconnect();
    activeTrial = null;
    runtime.setBehavior('idle');
    timeline.clear();
    session.analyzer.reset();
  }
}

/** 百分位使用固定最近秩，不丢弃缺失口型；缺失另计失败，避免幸存样本让结果虚高。 */
function percentile(values: number[], fraction: number): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
}

/** 软件输出时钟与屏幕参数测量明确声明范围，不把合成轨道当作物理麦克风或扬声器。 */
function report(includeTrace = true) {
  const openErrors = trials.flatMap((trial) =>
    trial.openingOffsetMs === null ? [] : [Math.abs(trial.openingOffsetMs)]
  );
  const tails = trials.flatMap((trial) =>
    trial.tailClosingOffsetMs === null ? [] : [Math.max(0, trial.tailClosingOffsetMs)]
  );
  const missing = trials.filter(
    (trial) => trial.actualOpenAt === null || trial.actualCloseAt === null
  ).length;
  const p95 = percentile(openErrors, 0.95);
  const maxTail = tails.length ? Math.max(...tails) : null;
  const paused = trials.flatMap((trial) => trial.pauseChecks);
  return {
    version: 1,
    createdAt,
    recordedAt: new Date().toISOString(),
    syntheticAudio: true,
    clock: 'AudioContext software output timestamp; not physical output loopback',
    humanDevicesVerified: false,
    actualPhonemeShapeHumanReviewed: false,
    source: '30 private CosyVoice WAV files; real Cubism Web runtime and MotionSync CRI',
    userAgent: navigator.userAgent,
    summary: {
      utterances: trials.length,
      missingMeasurements: missing,
      openingAbsoluteP95Ms: p95,
      maxTailClosingDelayMs: maxTail,
      pauseWindows: paused.length,
      allPauseWindowsClosed: paused.every((pause) => pause.allClosed),
      modes: [...modes],
      frameCount,
      fpsMedian: percentile(
        telemetry.filter((item) => !item.hidden).map((item) => item.fps),
        0.5
      ),
      fpsP05: percentile(
        telemetry.filter((item) => !item.hidden).map((item) => item.fps),
        0.05
      ),
      hiddenFrames,
      maxTimeline,
      maxPcmQueue,
      contextsCreated,
      contextsClosed,
      completedReconnectCycles: cycleCount,
      resourceCyclesPassed:
        cycleCount === 20 &&
        resources.every(
          (item) =>
            item.contextClosed &&
            item.liveNodesAfterDispose === 0 &&
            item.trackListenersAfterDispose === 0 &&
            item.queueAfterDispose === 0 &&
            item.originalTrackLiveBeforeHarnessStop
        ),
      telemetryBounded: telemetry.length <= 2_048 && maxTimeline <= 32 && maxPcmQueue <= 9_600,
      timingPassed:
        trials.length === 30 &&
        !missing &&
        p95 !== null &&
        p95 <= 100 &&
        maxTail !== null &&
        maxTail <= 150 &&
        modes.size === 1 &&
        modes.has('motion-sync') &&
        !failures.length,
    },
    failures: [...failures],
    trials: trials.map((trial) => (includeTrace ? trial : { ...trial, trace: undefined })),
    resources,
    telemetry,
    stability,
  };
}

/** 证据保存在固定本机测试目录，不触发浏览器下载，也不把运行参数传给产品接口。 */
async function save(name: 'report.json' | 'stability.json', result = report()) {
  const response = await fetch('/evidence/' + name, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(result),
  });
  if (!response.ok) throw new Error('证据保存失败');
}

/** 连续静音维持录制音轨的时间戳；不接扬声器，避免首句前无输入时声轨压掉等待时间。 */
function beginRecording() {
  if (!current || !('MediaRecorder' in window)) throw new Error('当前浏览器不支持带声音录像');
  const video = canvas.captureStream(60);
  const stream = new MediaStream([
    ...video.getVideoTracks(),
    ...current.destination.stream.getAudioTracks(),
  ]);
  const mimeType = 'video/webm;codecs=vp8,opus';
  const recorder = new MediaRecorder(stream, {
    mimeType: MediaRecorder.isTypeSupported(mimeType) ? mimeType : 'video/webm',
    videoBitsPerSecond: 1_800_000,
  });
  const chunks: Blob[] = [];
  recorder.addEventListener('dataavailable', (event) => {
    if (event.data.size) chunks.push(event.data);
  });
  const silence = current.audio.context.createConstantSource();
  silence.offset.value = 0;
  silence.connect(current.destination);
  silence.start();
  recorder.start(1_000);
  return {
    /** 先收完编码块再释放录制专用静音，既保留尾音也不把测试节点带到后续会话。 */
    async finish() {
      if (recorder.state !== 'inactive') {
        const stopped = new Promise<void>((resolve) =>
          recorder.addEventListener('stop', () => resolve(), { once: true })
        );
        recorder.stop();
        await stopped;
      }
      silence.stop();
      silence.disconnect();
      video.getTracks().forEach((track) => track.stop());
      const response = await fetch('/evidence/recording.webm', {
        method: 'POST',
        headers: { 'Content-Type': 'video/webm' },
        body: new Blob(chunks, { type: recorder.mimeType }),
      });
      if (!response.ok) throw new Error('录像保存失败');
      chunks.length = 0;
    },
  };
}

/** 按连续连接和挂断的真实所有权执行 20 次回收，不能只循环创建普通 JS 对象。 */
async function reconnectCycles(signal: AbortSignal) {
  for (let index = 0; index < 20; index++) {
    signal.throwIfAborted();
    await connectSession(signal);
    await play(manifest.utterances[0], signal, false);
    cycleCount++;
    await disconnectSession();
    updateStatus('口型测量完成，资源回收 ' + cycleCount + ' / 20');
  }
}

/** 录像先等待编码器稳定再播放首句；取消和异常走统一回收，不修改生产音频的时序。 */
async function run(kind: 'calibration' | 'stability') {
  if (controller) return;
  controller = new AbortController();
  const signal = controller.signal;
  runButton.disabled = stabilityButton.disabled = true;
  cancelButton.disabled = false;
  let recording: ReturnType<typeof beginRecording> | undefined;
  try {
    await connectSession(signal);
    if (kind === 'calibration') {
      trials.length = 0;
      recording = beginRecording();
      await new Promise<void>((resolve) => setTimeout(resolve, 2_000));
      for (const [index, utterance] of manifest.utterances.entries()) {
        updateStatus('合成口型测量 ' + (index + 1) + ' / 30 · ' + utterance.id);
        await play(utterance, signal);
      }
      await recording.finish();
      recording = undefined;
      await disconnectSession();
      await reconnectCycles(signal);
      await save('report.json');
    } else {
      const started = performance.now();
      stability = {
        startedAt: new Date().toISOString(),
        elapsedSeconds: 0,
        iterations: 0,
        complete: false,
      };
      while (performance.now() - started < 30 * 60_000) {
        signal.throwIfAborted();
        await play(manifest.utterances[stability.iterations % 30], signal, false);
        stability.iterations++;
        stability.elapsedSeconds = (performance.now() - started) / 1000;
        updateStatus('30 分钟稳定性 · 已运行 ' + Math.floor(stability.elapsedSeconds) + ' 秒');
        await save('stability.json');
      }
      stability.complete = true;
      await disconnectSession();
      await save('stability.json');
    }
    updateStatus('验收已完成，证据已保存到 .tools/live2d-verification/results');
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError'))
      failures.push(error instanceof Error ? error.message : '测试失败');
    updateStatus(signal.aborted ? '已取消，部分证据保留' : '验收失败：' + failures.at(-1));
  } finally {
    if (recording) await recording.finish().catch((error: Error) => failures.push(error.message));
    await disconnectSession();
    await save(kind === 'calibration' ? 'report.json' : 'stability.json').catch((error: Error) =>
      failures.push(error.message)
    );
    controller = null;
    runButton.disabled = stabilityButton.disabled = false;
    cancelButton.disabled = true;
    saveButton.disabled = false;
    document.body.dataset.state = 'ready';
    summary.textContent = JSON.stringify(report(false).summary, null, 2);
  }
}

/** 可见按钮是验收唯一入口，所有异步错误进入报告；不依赖调试器注入脚本。 */
async function initialize() {
  try {
    const response = await fetch('/utterances/manifest.json');
    if (!response.ok) throw new Error('30 条合成语音尚未准备完成');
    manifest = await response.json();
    if (!manifest.complete || !manifest.synthetic_audio || manifest.utterances.length !== 30)
      throw new Error('合成样本清单不完整');
    runtime = await Live2DRuntime.load(
      canvas,
      {
        /** 实际渲染使用软件输出时钟，受阻时保持闭嘴，状态文案不能驱动嘴部。 */
        readLip: () =>
          current?.audio.context.state === 'running'
            ? timeline.select(getAudibleAudioTime(current.audio.context))
            : null,
        /** 测试页把 GL 故障计为失败，不能用静态后备让性能测试继续显示成功。 */
        onFault: () => failures.push('WebGL 渲染故障'),
        /** 恢复事件留证，完整重载属于产品页面的独立异常验收。 */
        onRestore: () => failures.push('WebGL 上下文恢复，稳定性验收需重新执行'),
        onFrame,
      },
      new AbortController().signal
    );
    runButton.disabled = stabilityButton.disabled = false;
    updateStatus('正式模型已准备 · 30 条合成语音 · 软件时钟验收');
  } catch (error) {
    failures.push(error instanceof Error ? error.message : '初始化失败');
    updateStatus('初始化失败：' + failures.at(-1));
  }
}

runButton.addEventListener('click', () => void run('calibration'));
stabilityButton.addEventListener('click', () => void run('stability'));
cancelButton.addEventListener('click', () => controller?.abort());
saveButton.addEventListener(
  'click',
  () =>
    void save('report.json')
      .then(() => updateStatus('证据已保存'))
      .catch((error: Error) => updateStatus(error.message))
);
window.addEventListener('pagehide', () => {
  controller?.abort();
  void disconnectSession();
  runtime?.dispose();
});
void initialize();
