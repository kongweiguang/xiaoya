import type { CubismModel } from '@framework/model/cubismmodel';
import { AudioLevelLipSync, type LipSyncFrame, rmsAmplitude } from './lip-sync';

type MotionSyncMode = 'motion-sync' | 'amplitude';

type MotionSyncBackend = {
  sampleRate: number;
  getRequiredSamples: () => number;
  analyze: (samples: Float32Array) => { consumed: number; open: number; form: number };
  reset: () => void;
  dispose: () => void;
};

type MotionSyncAnalyzerOptions = {
  backend?: MotionSyncBackend | null;
  onModeChange?: (mode: MotionSyncMode, error?: Error) => void;
};

const TARGET_SAMPLE_RATE = 48_000;
const MAX_BUFFER_SECONDS = 0.2;

/** 归一化只约束 SDK 参数范围，不改变音频本身的采样时钟。 */
function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

/** 适配器故障保留原始原因，页面与验收能区分官方分析和音量后备。 */
function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error('MotionSync 分析失败', { cause: error });
}

export class StreamingPcmResampler {
  private readonly targetRate: number;
  private sourceRate = 0;
  private inputCount = 0;
  private outputCount = 0;
  private previous = 0;

  /** 保留跨窗口的插值相位，44.1 kHz 输入不会因逐块取整长期漂移。 */
  constructor(targetRate = TARGET_SAMPLE_RATE) {
    if (!Number.isFinite(targetRate) || targetRate <= 0) {
      throw new RangeError('重采样频率必须为正数');
    }
    this.targetRate = targetRate;
  }

  /** 每次只保留最后一个输入值；需要下一窗口的插值点延后输出，内存不随通话增长。 */
  push(samples: Float32Array, sourceRate: number): Float32Array {
    if (!Number.isFinite(sourceRate) || sourceRate <= 0) {
      throw new RangeError('输入采样率必须为正数');
    }
    if (samples.length === 0) return new Float32Array();
    if (this.sourceRate !== sourceRate) {
      this.reset();
      this.sourceRate = sourceRate;
    }
    const ratio = sourceRate / this.targetRate;
    const start = this.inputCount;
    const end = start + samples.length;
    const values: number[] = [];
    while (true) {
      const position = this.outputCount * ratio;
      const leftIndex = Math.floor(position);
      const fraction = position - leftIndex;
      if (leftIndex >= end || (fraction > 1e-9 && leftIndex + 1 >= end)) break;
      const left = leftIndex < start ? this.previous : samples[leftIndex - start];
      const right = leftIndex + 1 >= end ? left : samples[leftIndex + 1 - start];
      const value = left + (right - left) * fraction;
      values.push(Number.isFinite(value) ? clamp(value, -1, 1) : 0);
      this.outputCount += 1;
    }
    this.previous = samples[samples.length - 1];
    this.inputCount = end;
    return Float32Array.from(values);
  }

  /** 时钟不连续和轨道替换时忘记插值尾值，不能把上一句话接到新轨道。 */
  reset(): void {
    this.sourceRate = 0;
    this.inputCount = 0;
    this.outputCount = 0;
    this.previous = 0;
  }

  /** 输出计数用于计算缓冲起点，避免把分析完成时间错误当作音节时间。 */
  get producedSamples(): number {
    return this.outputCount;
  }
}

export class MotionSyncAnalyzer {
  private backend: MotionSyncBackend | null;
  private readonly fallback = new AudioLevelLipSync();
  private readonly resampler: StreamingPcmResampler;
  private readonly onModeChange: MotionSyncAnalyzerOptions['onModeChange'];
  private pending = new Float32Array();
  private pendingAt = 0;
  private originAt: number | null = null;
  private previousEnd: number | null = null;
  private previousRate: number | null = null;
  private lastAt = -Infinity;
  private disposed = false;

  /** 官方引擎是可替换的外部端口，纯时序逻辑可在离线测试中检查消费数量与回收。 */
  constructor(options: MotionSyncAnalyzerOptions = {}) {
    this.backend = options.backend ?? null;
    this.onModeChange = options.onModeChange;
    const targetRate = this.backend?.sampleRate ?? TARGET_SAMPLE_RATE;
    if (!Number.isFinite(targetRate) || targetRate < 16_000 || targetRate > 128_000) {
      throw new RangeError('MotionSync 输入频率必须在官方支持范围内');
    }
    this.resampler = new StreamingPcmResampler(targetRate);
  }

  /** 按原生引擎真实消费量切块并保留窗口中心时间，不能把整批声音压成最后一个嘴型。 */
  sample(samples: Float32Array, sampleRate: number, at: number): LipSyncFrame[] {
    if (
      this.disposed ||
      !Number.isFinite(at) ||
      at < 0 ||
      at <= this.lastAt ||
      !Number.isFinite(sampleRate) ||
      sampleRate < 8_000 ||
      sampleRate > 192_000 ||
      samples.length === 0 ||
      samples.length > sampleRate * MAX_BUFFER_SECONDS
    ) {
      return [];
    }
    const start = at - samples.length / sampleRate / 2;
    if (
      this.previousRate !== null &&
      (this.previousRate !== sampleRate ||
        (this.previousEnd !== null && Math.abs(start - this.previousEnd) > 0.002))
    ) {
      this.reset();
    }
    this.lastAt = at;
    this.previousRate = sampleRate;
    this.previousEnd = start + samples.length / sampleRate;
    const levelFrame = this.fallback.sample(samples, sampleRate, at);
    if (!this.backend) return [levelFrame];
    try {
      const rate = this.backend.sampleRate;
      const producedBefore = this.resampler.producedSamples;
      const resampled = this.resampler.push(samples, sampleRate);
      this.originAt ??= start;
      if (this.pending.length === 0) this.pendingAt = this.originAt + producedBefore / rate;
      const maximum = Math.ceil(rate * MAX_BUFFER_SECONDS);
      if (this.pending.length + resampled.length > maximum) {
        this.backend.reset();
        this.pending = new Float32Array();
        this.pendingAt = this.originAt + producedBefore / rate;
      }
      const joined = new Float32Array(this.pending.length + resampled.length);
      joined.set(this.pending);
      joined.set(resampled, this.pending.length);
      this.pending = joined;
      const frames: LipSyncFrame[] = [];
      while (this.backend) {
        const required = this.backend.getRequiredSamples();
        if (!Number.isInteger(required) || required < 1 || required > maximum) {
          throw new Error('MotionSync 返回了无效的分析窗口');
        }
        if (this.pending.length < required) break;
        const block = this.pending.slice(0, required);
        const result = this.backend.analyze(block);
        if (
          !Number.isInteger(result.consumed) ||
          result.consumed <= 0 ||
          result.consumed > block.length ||
          !Number.isFinite(result.open) ||
          !Number.isFinite(result.form)
        ) {
          throw new Error('MotionSync 返回了无效的分析结果');
        }
        const frameAt = this.pendingAt + result.consumed / rate / 2;
        const silent = rmsAmplitude(block.subarray(0, result.consumed)) <= 0.012;
        frames.push({
          at: frameAt,
          open: silent ? 0 : clamp(result.open, 0, 1),
          form: silent ? 0 : clamp(result.form, -1, 1),
        });
        this.pending = this.pending.slice(result.consumed);
        this.pendingAt += result.consumed / rate;
      }
      return frames;
    } catch (error) {
      this.useFallback(error);
      return [levelFrame];
    }
  }

  /** 取消、静音许可变化和音轨替换时清除原生历史及不足一帧的 PCM。 */
  reset(): void {
    this.pending = new Float32Array();
    this.originAt = null;
    this.previousEnd = null;
    this.previousRate = null;
    this.pendingAt = 0;
    this.lastAt = -Infinity;
    this.resampler.reset();
    this.fallback.reset();
    try {
      this.backend?.reset();
    } catch (error) {
      this.useFallback(error);
    }
  }

  /** 原生资源有明确所有者，重复卸载不会重复释放共享引擎或当前上下文。 */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    const backend = this.backend;
    this.backend = null;
    this.reset();
    backend?.dispose();
  }

  /** 验收与 UI 必须能区分音量后备，不能把后备结果描述成官方 MotionSync 通过。 */
  get mode(): MotionSyncMode {
    return this.backend ? 'motion-sync' : 'amplitude';
  }

  /** 只暴露数量用于诊断有界性，不允许外层修改正在等待原生分析的 PCM。 */
  get queuedSamples(): number {
    return this.pending.length;
  }

  /** 官方分析失败后释放其资源并保留音量历史，人物仍跟随声音开合但明确标记降级。 */
  private useFallback(error: unknown): void {
    const backend = this.backend;
    this.backend = null;
    this.pending = new Float32Array();
    let failure = asError(error);
    try {
      backend?.dispose();
    } catch (cleanupError) {
      failure = new AggregateError([failure, asError(cleanupError)], 'MotionSync 分析及释放失败');
    }
    this.onModeChange?.('amplitude', failure);
  }
}

/** 延迟导入让 SSR 安全；取消在读取模型前生效，不能把已结束会话变成音量后备或读取已销毁模型。 */
export async function createMotionSyncAnalyzer(
  model: CubismModel,
  buffer: ArrayBuffer,
  options: Pick<MotionSyncAnalyzerOptions, 'onModeChange'> & { signal?: AbortSignal } = {}
): Promise<MotionSyncAnalyzer> {
  options.signal?.throwIfAborted();
  try {
    if (typeof Live2DCubismMotionSyncCore === 'undefined') {
      throw new Error('MotionSync Core 尚未加载');
    }
    const [{ CubismMotionSyncData }, { CubismMotionSyncEngineController }, { csmVector }] =
      await Promise.all([
        import('./vendor/motionsync/src/cubismmotionsyncdata.js'),
        import('./vendor/motionsync/src/cubismmotionsyncenginecontroller.js'),
        import('@framework/type/csmvector'),
      ]);
    options.signal?.throwIfAborted();
    const data = CubismMotionSyncData.create(model, buffer, buffer.byteLength);
    let processor:
      | import('./vendor/motionsync/src/cubismmotionsyncprocessorcri').CubismMotionSyncProcessorCRI
      | null = null;
    let result:
      | import('./vendor/motionsync/src/cubismmotionsyncengineanalysisresult').CubismMotionSyncEngineAnalysisResult
      | null = null;
    const engine =
      CubismMotionSyncEngineController.getEngine(0) ??
      CubismMotionSyncEngineController.initializeEngine(0);
    try {
      if (!engine) throw new Error('MotionSync CRI 引擎初始化失败');
      let settingIndex = -1;
      for (let index = 0; index < data.getSettingCount(); index += 1) {
        const candidate = data.getSetting(index);
        if (candidate.analysisType === 0 && candidate.useCase === 0) {
          settingIndex = index;
          break;
        }
      }
      if (settingIndex < 0) throw new Error('模型缺少 CRI Mouth 分析配置');
      const setting = data.getSetting(settingIndex);
      const parameters = setting.cubismParameterList;
      let openIndex = -1;
      let formIndex = -1;
      for (let index = 0; index < parameters.getSize(); index += 1) {
        const parameter = parameters.at(index);
        if (parameter.parameterIndex < 0) continue;
        if (parameter.id.s === 'ParamMouthOpenY') openIndex = index;
        if (parameter.id.s === 'ParamMouthForm') formIndex = index;
      }
      if (openIndex < 0 || formIndex < 0) throw new Error('模型缺少开合或嘴型形变参数');
      processor = (
        engine as import('./vendor/motionsync/src/cubismmotionsyncenginecri').CubismMotionSyncEngineCri
      ).CreateProcessor(
        parameters.getSize(),
        data.getMappingInfoList(settingIndex),
        TARGET_SAMPLE_RATE
      );
      if (!processor || processor.getRequireSampleCount() <= 0) {
        throw new Error('MotionSync 原生分析上下文不可用');
      }
      result = processor.createAnalysisResult();
      let smoothedOpen = 0;
      let smoothedForm = 0;
      let released = false;
      const nativeProcessor = processor;
      const nativeResult = result;
      /** 还原模型配置中的平滑与阻尼，保持官方嘴型标定的含义。 */
      const smoothValue = (value: number, previous: number, index: number): number => {
        const parameter = parameters.at(index);
        const smoothing = clamp(parameter.smooth, 0, 100) / 100;
        const next = value * (1 - smoothing) + previous * smoothing;
        return Math.abs(next - previous) < parameter.damper ? previous : next;
      };
      return new MotionSyncAnalyzer({
        ...options,
        backend: {
          sampleRate: TARGET_SAMPLE_RATE,
          /** 需求数量可能随引擎状态变化，逐块查询而不缓存第一帧的值。 */
          getRequiredSamples: () => nativeProcessor.getRequireSampleCount(),
          /** 直接读取官方结果，不让分析时的临时参数覆盖当前正在渲染的模型。 */
          analyze(samples) {
            const vector = new csmVector<number>();
            for (const value of samples) vector.pushBack(value);
            const analyzed = nativeProcessor.Analyze(
              vector,
              0,
              setting.blendRatio,
              setting.smoothing,
              0,
              nativeResult
            );
            if (!analyzed) throw new Error('MotionSync CRI 音频分析失败');
            const values = analyzed.getValues();
            smoothedOpen = smoothValue(values[openIndex], smoothedOpen, openIndex);
            smoothedForm = smoothValue(values[formIndex], smoothedForm, formIndex);
            const openParameter = parameters.at(openIndex);
            const formParameter = parameters.at(formIndex);
            return {
              consumed: analyzed.getProcessedSampleCount(),
              open: (smoothedOpen - openParameter.min) / (openParameter.max - openParameter.min),
              form:
                ((smoothedForm - formParameter.min) / (formParameter.max - formParameter.min)) * 2 -
                1,
            };
          },
          /** 原生 clear 保留已分配上下文，频繁打断不重新分配映射和引擎。 */
          reset() {
            nativeProcessor.getContextHandle().getContext().csmMotionSyncClear();
            smoothedOpen = 0;
            smoothedForm = 0;
          },
          /** 先释放每个上下文，最后一个消费者离开后才销毁共享引擎。 */
          dispose() {
            if (released) return;
            released = true;
            try {
              nativeResult.release();
            } finally {
              try {
                nativeProcessor.Close();
              } finally {
                data.release();
                CubismMotionSyncEngineController.releaseEngineNotForce(engine);
              }
            }
          },
        },
      });
    } catch (error) {
      result?.release();
      processor?.Close();
      data.release();
      if (engine) CubismMotionSyncEngineController.releaseEngineNotForce(engine);
      throw error;
    }
  } catch (error) {
    options.signal?.throwIfAborted();
    if (error instanceof Error && error.name === 'AbortError') throw error;
    options.onModeChange?.('amplitude', asError(error));
    return new MotionSyncAnalyzer(options);
  }
}
