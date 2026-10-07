export type LipSyncFrame = {
  /** 音频窗口中心对应的 AudioContext 秒数，不能混用墙上时钟。 */
  at: number;
  open: number;
  form: number;
};

export type LipSyncTimelineOptions = {
  maxFrames?: number;
  maxAge?: number;
};

export type AudioLevelLipSyncOptions = {
  silenceThreshold?: number;
  fullOpenRms?: number;
  attackSeconds?: number;
  releaseSeconds?: number;
};

type AudioClock = {
  currentTime: number;
  baseLatency?: number;
  outputLatency?: number;
  getOutputTimestamp?: () => { contextTime?: number; performanceTime?: number };
};

/** 参数归一化统一放在纯逻辑层，避免异常模型输出扩散到 WebGL 参数。 */
function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

/** 闭嘴帧保留当前音频时间，便于诊断而不继续复用过期的开口结果。 */
function closedFrame(at: number): LipSyncFrame {
  return { at: Number.isFinite(at) ? at : 0, open: 0, form: 0 };
}

/** 在输出设备实际播放的位置取样，避免设备缓冲把口型提前到用户听到声音之前。 */
export function getAudibleAudioTime(context: AudioClock, now = performance.now()): number {
  if (context.getOutputTimestamp) {
    try {
      const timestamp = context.getOutputTimestamp();
      const { contextTime, performanceTime } = timestamp;
      if (
        contextTime !== undefined &&
        performanceTime !== undefined &&
        Number.isFinite(contextTime) &&
        Number.isFinite(performanceTime) &&
        contextTime > 0 &&
        performanceTime > 0 &&
        now >= performanceTime &&
        now - performanceTime < 1_000
      ) {
        return clamp(contextTime + (now - performanceTime) / 1_000, 0, context.currentTime);
      }
    } catch {
      // 部分浏览器暴露此方法但不支持当前输出设备，使用已公开的延迟值继续校准。
    }
  }
  const base = context.baseLatency ?? 0;
  const output = context.outputLatency ?? 0;
  const latency =
    (Number.isFinite(base) ? Math.max(0, base) : 0) +
    (Number.isFinite(output) ? Math.max(0, output) : 0);
  return Math.max(0, context.currentTime - latency);
}

/** 用短窗口能量作为降级信号；无效 PCM 当作静音，防止一次 NaN 污染后续平滑状态。 */
export function rmsAmplitude(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let energy = 0;
  for (const sample of samples) {
    const value = Number.isFinite(sample) ? clamp(sample, -1, 1) : 0;
    energy += value * value;
  }
  return Math.sqrt(energy / samples.length);
}

export class LipSyncTimeline {
  private frames: LipSyncFrame[] = [];
  private held: LipSyncFrame | null = null;
  private latestAt = -Infinity;
  private readonly maxFrames: number;
  private readonly maxAge: number;

  /** 队列按少量短音频窗口配置，后台恢复时也不能积累一整段旧嘴型。 */
  constructor(options: LipSyncTimelineOptions = {}) {
    const maxFrames = options.maxFrames ?? 32;
    const maxAge = options.maxAge ?? 0.12;
    if (!Number.isInteger(maxFrames) || maxFrames < 1) {
      throw new RangeError('口型队列容量必须为正整数');
    }
    if (!Number.isFinite(maxAge) || maxAge <= 0) {
      throw new RangeError('口型有效时长必须为正数');
    }
    this.maxFrames = maxFrames;
    this.maxAge = maxAge;
  }

  /** 拒绝迟到和损坏结果；同一时间的修正帧覆盖原帧，不增加缓冲长度。 */
  push(frame: LipSyncFrame): boolean {
    if (
      !Number.isFinite(frame.at) ||
      frame.at < 0 ||
      !Number.isFinite(frame.open) ||
      !Number.isFinite(frame.form) ||
      frame.at < this.latestAt
    ) {
      return false;
    }
    const next = { at: frame.at, open: clamp(frame.open, 0, 1), form: clamp(frame.form, -1, 1) };
    const last = this.frames[this.frames.length - 1];
    if (last?.at === frame.at) {
      this.frames[this.frames.length - 1] = next;
    } else if (this.held?.at === frame.at && this.frames.length === 0) {
      this.held = next;
    } else {
      this.frames.push(next);
      if (this.frames.length > this.maxFrames) this.frames.shift();
    }
    this.latestAt = frame.at;
    return true;
  }

  /** 只展示已经到输出时钟的帧；停止采样后自动收嘴，未来帧留在队列等待播放。 */
  select(audibleAt: number): LipSyncFrame {
    if (!Number.isFinite(audibleAt)) return closedFrame(0);
    while (this.frames.length > 0 && this.frames[0].at <= audibleAt) {
      this.held = this.frames.shift()!;
    }
    if (!this.held || this.held.at > audibleAt || audibleAt - this.held.at > this.maxAge) {
      return closedFrame(audibleAt);
    }
    return this.held;
  }

  /** 轨道替换和挂断必须同时忘记当前帧及顺序栅栏，新会话可以从新时钟开始。 */
  clear(): void {
    this.frames = [];
    this.held = null;
    this.latestAt = -Infinity;
  }

  /** 暴露待播数量供诊断与有界性验证，不把内部数组交给调用方修改。 */
  get size(): number {
    return this.frames.length;
  }
}

export class AudioLevelLipSync {
  private open = 0;
  private lastAt: number | null = null;
  private silentSince: number | null = null;
  private readonly silenceThreshold: number;
  private readonly fullOpenRms: number;
  private readonly attackSeconds: number;
  private readonly releaseSeconds: number;

  /** 音量驱动仅负责可靠降级；固定快开慢收可压低嘴部抖动，并保留短停顿。 */
  constructor(options: AudioLevelLipSyncOptions = {}) {
    this.silenceThreshold = options.silenceThreshold ?? 0.012;
    this.fullOpenRms = options.fullOpenRms ?? 0.18;
    this.attackSeconds = options.attackSeconds ?? 0.025;
    this.releaseSeconds = options.releaseSeconds ?? 0.045;
    if (
      !Number.isFinite(this.silenceThreshold) ||
      this.silenceThreshold < 0 ||
      !Number.isFinite(this.fullOpenRms) ||
      this.fullOpenRms <= this.silenceThreshold ||
      !Number.isFinite(this.attackSeconds) ||
      this.attackSeconds <= 0 ||
      !Number.isFinite(this.releaseSeconds) ||
      this.releaseSeconds <= 0
    ) {
      throw new RangeError('口型能量阈值和过渡时长无效');
    }
  }

  /** 使用窗口时间而非渲染帧率平滑，低帧率设备也保持一致的开口与闭口速度。 */
  sample(samples: Float32Array, sampleRate: number, at: number): LipSyncFrame {
    if (!Number.isFinite(at) || at < 0 || !Number.isFinite(sampleRate) || sampleRate <= 0) {
      return closedFrame(at);
    }
    if (this.lastAt !== null && at <= this.lastAt) {
      return { at: this.lastAt, open: this.open, form: 0 };
    }
    const elapsed = this.lastAt === null ? samples.length / sampleRate : at - this.lastAt;
    const rms = rmsAmplitude(samples);
    const silent = rms <= this.silenceThreshold;
    const target = silent
      ? 0
      : clamp((rms - this.silenceThreshold) / (this.fullOpenRms - this.silenceThreshold), 0, 1);
    const duration = target > this.open ? this.attackSeconds : this.releaseSeconds;
    const smoothing = 1 - Math.exp(-elapsed / duration);
    this.open += (target - this.open) * smoothing;
    this.silentSince = silent ? (this.silentSince ?? at) : null;
    if (this.open < 0.005 || (this.silentSince !== null && at - this.silentSince >= 0.08)) {
      this.open = 0;
    }
    this.lastAt = at;
    return { at, open: this.open, form: 0 };
  }

  /** 断线与禁止播放时直接清零，避免重新连接后的第一个窗口继承上一句话。 */
  reset(): void {
    this.open = 0;
    this.lastAt = null;
    this.silentSince = null;
  }
}
