import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { registerHooks } from 'node:module';
import { dirname, extname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

registerHooks({
  /** 仅补齐 Node 的本地 alias 和扩展名解析，正式浏览器继续使用 Next.js 编译配置。 */
  resolve(specifier, context, nextResolve) {
    let url;
    if (specifier.startsWith('@framework/')) {
      url = new URL(
        `../lib/avatar/vendor/cubism/src/${specifier.slice('@framework/'.length)}.js`,
        import.meta.url
      );
    } else if (specifier.startsWith('.') && context.parentURL) {
      const candidate = new URL(specifier, context.parentURL);
      if (!extname(candidate.pathname)) {
        for (const extension of ['.ts', '.js']) {
          const resolved = new URL(candidate.href + extension);
          if (existsSync(fileURLToPath(resolved))) {
            url = resolved;
            break;
          }
        }
      }
    }
    return url ? { url: url.href, shortCircuit: true } : nextResolve(specifier, context);
  },
});

const { createMotionSyncAnalyzer } = await import('../lib/avatar/motion-sync.ts');
const { LipSyncTimeline, rmsAmplitude } = await import('../lib/avatar/lip-sync.ts');
const projectRoot = fileURLToPath(new URL('../../', import.meta.url));
const AUDIO_THRESHOLD = 0.012;
const OPEN_THRESHOLD = 0.03;
const CLOSED_THRESHOLD = 0.01;

/** 文件指纹把测量绑定到本次最终模型和实际音频，防止旧结果被当作新实现证据。 */
function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/** WAV 保留原采样率并独立解析容器，不通过浏览器解码来冒充本脚本的设备测量。 */
async function readWav(path) {
  const bytes = await readFile(path);
  if (bytes.toString('ascii', 0, 4) !== 'RIFF' || bytes.toString('ascii', 8, 12) !== 'WAVE') {
    throw new Error('测试音频不是 WAV');
  }
  let rate;
  let data;
  for (let offset = 12; offset + 8 <= bytes.length; ) {
    const size = bytes.readUInt32LE(offset + 4);
    const start = offset + 8;
    if (start + size > bytes.length) throw new Error('WAV 音频容器不完整');
    const id = bytes.toString('ascii', offset, offset + 4);
    if (id === 'fmt ') {
      if (
        size < 16 ||
        bytes.readUInt16LE(start) !== 1 ||
        bytes.readUInt16LE(start + 2) !== 1 ||
        bytes.readUInt16LE(start + 14) !== 16
      ) {
        throw new Error('测试音频必须为单声道 PCM16');
      }
      rate = bytes.readUInt32LE(start + 4);
    } else if (id === 'data') {
      data = bytes.subarray(start, start + size);
    }
    offset = start + size + (size % 2);
  }
  if (!rate || !data || data.length % 2) throw new Error('WAV 缺少完整的采样信息');
  const samples = new Float32Array(data.length / 2);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = data.readInt16LE(index * 2) / 32768;
  }
  return { samples, rate, sha256: sha256(bytes) };
}

/** 统计采用最近秩，三十条用例的 P95 是第 29 个值，不进行隐藏的离群值剔除。 */
function percentile(values, fraction) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
}

/** 原生引擎仍在受控 VM 中真实执行，缺少 Node 支持的浏览器环境不会触发网络下载。 */
async function initializeSdk() {
  const sandbox = vm.createContext({
    console,
    atob,
    performance,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  });
  for (const name of ['live2dcubismcore.min.js', 'live2dcubismmotionsynccore.min.js']) {
    const source = await readFile(
      new URL(`../public/avatar/vendor/${name}`, import.meta.url),
      'utf8'
    );
    try {
      vm.runInContext(source, sandbox, { filename: name });
    } catch (error) {
      throw new Error(`官方 Core 初始化失败：${error.message}`);
    }
  }
  globalThis.Live2DCubismCore = sandbox.Live2DCubismCore;
  globalThis.Live2DCubismMotionSyncCore = sandbox.Live2DCubismMotionSyncCore;
  const { CubismFramework, LogLevel } = await import(
    '../lib/avatar/vendor/cubism/src/live2dcubismframework.js'
  );
  await import('../lib/avatar/vendor/cubism/src/rendering/cubismrenderer_webgl.js');
  if (!CubismFramework.startUp({ loggingLevel: LogLevel.LogLevel_Off })) {
    throw new Error('官方 Cubism Framework 初始化失败');
  }
  CubismFramework.initialize();
  return CubismFramework;
}

/** 能量基准使用独立的 10 ms 窗口，不拿收到字幕或 SDK speaking 状态当作声音起点。 */
function audioActivity(samples, rate) {
  const size = Math.round(rate * 0.01);
  const activity = [];
  for (let offset = 0; offset < samples.length; offset += size) {
    const window = samples.subarray(offset, offset + size);
    activity.push({
      start: offset / rate,
      end: (offset + window.length) / rate,
      active: rmsAmplitude(window) > AUDIO_THRESHOLD,
    });
  }
  return activity;
}

/** 理想 60 Hz 绘制只使用已经完整收到的 PCM，保留采集窗口带来的真实算法等待。 */
function analyzeWave(analyzer, samples, rate) {
  analyzer.reset();
  const timeline = new LipSyncTimeline();
  const size = Math.round(rate * 0.02);
  const trace = [];
  let renderAt = 0;
  /** 按理想绘制时钟读取有界队列，记录 mouth 参数，不输出或重新编码音频。 */
  const renderUntil = (end) => {
    while (renderAt < end - 1e-9) {
      const frame = timeline.select(renderAt);
      trace.push({ at: renderAt, open: frame.open, form: frame.form });
      renderAt += 1 / 60;
    }
  };
  for (let offset = 0; offset < samples.length; offset += size) {
    const chunk = new Float32Array(size);
    chunk.set(samples.subarray(offset, offset + size));
    const end = (offset + size) / rate;
    renderUntil(end);
    for (const frame of analyzer.sample(chunk, rate, offset / rate + size / rate / 2)) {
      timeline.push(frame);
    }
    if (analyzer.mode !== 'motion-sync') throw new Error('原生分析发生降级，不能计为同步通过');
  }
  renderUntil(samples.length / rate + 0.25);
  return trace;
}

/** 闭口需要连续三个绘制时刻稳定，避免把瞬时嘴型收窄误算成真正停音后的闭嘴。 */
function closedAfter(trace, start, end = Infinity) {
  for (let index = 0; index < trace.length - 2; index += 1) {
    if (trace[index].at < start || trace[index + 2].at >= end) continue;
    if (
      trace[index].open <= CLOSED_THRESHOLD &&
      trace[index + 1].open <= CLOSED_THRESHOLD &&
      trace[index + 2].open <= CLOSED_THRESHOLD
    ) {
      return trace[index].at;
    }
  }
  return null;
}

/** 缺少有声或可见开口仍作为失败记录，不能为了好看的百分位删掉失败样本。 */
function timings(entry, activity, trace) {
  const active = activity.filter((window) => window.active);
  const firstActive = active[0]?.start ?? null;
  const lastActive = active.at(-1)?.end ?? null;
  const firstOpen = trace.find((frame) => frame.open > OPEN_THRESHOLD)?.at ?? null;
  const close = lastActive === null ? null : closedAfter(trace, lastActive);
  const onset =
    firstOpen === null || firstActive === null ? null : (firstOpen - firstActive) * 1000;
  const delay = close === null || lastActive === null ? null : (close - lastActive) * 1000;
  const pauses = [];
  for (const pause of entry.inserted_silences) {
    const start = pause.start_seconds;
    const end = start + pause.duration_ms / 1000;
    const preceding = active.filter((window) => window.end <= start).at(-1);
    const following = active.find((window) => window.start >= end);
    const closed = preceding ? closedAfter(trace, preceding.end, following?.start ?? end) : null;
    pauses.push({
      inserted_ms: pause.duration_ms,
      before_audio_end: preceding?.end ?? null,
      after_audio_start: following?.start ?? null,
      closed_at: closed,
      close_delay_ms: closed === null ? null : (closed - preceding.end) * 1000,
    });
  }
  return {
    id: entry.id,
    type: entry.type,
    file: entry.file,
    sample_rate: entry.sample_rate,
    duration_seconds: entry.duration_seconds,
    maximum_mouth_open: Math.max(...trace.map((frame) => frame.open)),
    mouth_form_range: [
      Math.min(...trace.map((frame) => frame.form)),
      Math.max(...trace.map((frame) => frame.form)),
    ],
    audio_onset_seconds: firstActive,
    mouth_onset_seconds: firstOpen,
    onset_error_ms: onset,
    onset_absolute_error_ms: onset === null ? null : Math.abs(onset),
    audio_stop_seconds: lastActive,
    mouth_closed_seconds: close,
    close_delay_ms: delay,
    visible_open_observed: firstOpen !== null,
    passed:
      onset !== null &&
      Math.abs(onset) <= 100 + 1e-6 &&
      delay !== null &&
      delay <= 150 + 1e-6 &&
      pauses.every((pause) => pause.close_delay_ms !== null && pause.close_delay_ms <= 150 + 1e-6),
    pauses,
  };
}

/** 固定文件及完整三十条是验收前提，本脚本结果始终明确标注非浏览器及非设备证据。 */
async function main() {
  const arguments_ = process.argv.slice(2);
  /** CLI 参数只有路径，没有模型密钥或服务地址，便于重复运行与归档。 */
  const argument = (name, fallback) => {
    const index = arguments_.indexOf(name);
    return index < 0 ? fallback : arguments_[index + 1];
  };
  const input = resolve(
    argument('--input', resolve(projectRoot, '.tools/live2d-verification/utterances'))
  );
  const output = resolve(
    argument(
      '--output',
      resolve(projectRoot, '.tools/live2d-verification/offline-motion-sync.json')
    )
  );
  const modelPath = resolve(
    argument('--model', resolve(projectRoot, 'web/public/avatar/xiaoya/xiaoya.model3.json'))
  );
  const manifestBytes = await readFile(resolve(input, 'manifest.json'));
  const manifest = JSON.parse(manifestBytes);
  if (!manifest.complete || manifest.utterances.length < 30 || !manifest.synthetic_audio) {
    throw new Error('测量必须使用完整且不少于三十条的合成语音清单');
  }
  const modelBytes = await readFile(modelPath);
  const references = JSON.parse(modelBytes).FileReferences;
  if (!references.MotionSync) throw new Error('正式模型清单缺少 MotionSync 配置');
  const mocBytes = await readFile(resolve(dirname(modelPath), references.Moc));
  const syncBytes = await readFile(resolve(dirname(modelPath), references.MotionSync));
  const framework = await initializeSdk();
  const { CubismMoc } = await import('../lib/avatar/vendor/cubism/src/model/cubismmoc.js');
  const moc = CubismMoc.create(
    mocBytes.buffer.slice(mocBytes.byteOffset, mocBytes.byteOffset + mocBytes.byteLength),
    true
  );
  if (!moc) throw new Error('实际模型 MOC 一致性校验失败');
  const model = moc.createModel();
  if (!model) {
    moc.release();
    throw new Error('实际模型初始化失败');
  }
  let analyzer;
  try {
    analyzer = await createMotionSyncAnalyzer(
      model,
      syncBytes.buffer.slice(syncBytes.byteOffset, syncBytes.byteOffset + syncBytes.byteLength)
    );
    if (analyzer.mode !== 'motion-sync') throw new Error('原生 MotionSync 初始化失败');
    const cases = [];
    for (const entry of manifest.utterances) {
      const wav = await readWav(resolve(input, entry.file));
      if (wav.sha256 !== entry.sha256 || wav.samples.length !== entry.frames) {
        throw new Error('测试音频与生成清单的指纹或长度不一致');
      }
      const trace = analyzeWave(analyzer, wav.samples, wav.rate);
      cases.push(timings(entry, audioActivity(wav.samples, wav.rate), trace));
    }
    const onsets = cases
      .map((entry) => entry.onset_absolute_error_ms)
      .filter((value) => value !== null);
    const closes = cases.map((entry) => entry.close_delay_ms).filter((value) => value !== null);
    const result = {
      kind: 'offline-native-analysis',
      synthetic_audio: true,
      browser_verified: false,
      human_devices_verified: false,
      excludes: [
        'browser-scheduling',
        'real-output-latency',
        'room-transport',
        'phoneme-visual-review',
      ],
      utterance_count: cases.length,
      observed_onset_count: onsets.length,
      observed_close_count: closes.length,
      algorithm: {
        motion_sync_sdk: '5-r.2',
        audio_rms_threshold: AUDIO_THRESHOLD,
        mouth_open_threshold: OPEN_THRESHOLD,
        mouth_closed_threshold: CLOSED_THRESHOLD,
        audio_reference_window_ms: 10,
        pcm_delivery_window_ms: 20,
        ideal_render_fps: 60,
      },
      fingerprints: {
        utterance_manifest_sha256: sha256(manifestBytes),
        model_manifest_sha256: sha256(modelBytes),
        moc_sha256: sha256(mocBytes),
        motion_sync_config_sha256: sha256(syncBytes),
      },
      onset_absolute_p95_ms: percentile(onsets, 0.95),
      onset_absolute_max_ms: onsets.length ? Math.max(...onsets) : null,
      close_p95_ms: percentile(closes, 0.95),
      close_max_ms: closes.length ? Math.max(...closes) : null,
      passed_case_count: cases.filter((entry) => entry.passed).length,
      explicit_pause_count: cases.reduce((count, entry) => count + entry.pauses.length, 0),
      pause_close_max_ms: Math.max(
        ...cases.flatMap((entry) => entry.pauses.map((pause) => pause.close_delay_ms ?? Infinity))
      ),
      cases,
    };
    result.passed =
      onsets.length === cases.length &&
      closes.length === cases.length &&
      result.onset_absolute_p95_ms <= 100 + 1e-6 &&
      result.close_max_ms <= 150 + 1e-6 &&
      result.pause_close_max_ms <= 150 + 1e-6;
    await mkdir(dirname(output), { recursive: true });
    await writeFile(output, JSON.stringify(result, null, 2));
    console.log(
      JSON.stringify({ ...result, cases: undefined, fingerprints: undefined, excludes: undefined })
    );
  } finally {
    analyzer?.dispose();
    moc.deleteModel(model);
    moc.release();
    framework.dispose();
  }
}

await main();
