import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFile, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { isAbsolute, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = fileURLToPath(new URL('../../', import.meta.url));
let results;
let baseline;
let reportName;
let recordingName;
let evidence;
let recording;
const warnings = [];

/** 输出与 ROI 必须显式指定，模型几何变化不能静默复用旧证据路径或旧嘴部位置。 */
export function parseOptions(arguments_) {
  const options = {
    baseline: false,
    results: resolve(root, '.tools/live2d-verification/results'),
    evidence: null,
    crop: null,
    closedBaseline: { start: 0.1, end: 0.35 },
    minimumRowPixels: 3,
    openHeightDelta: 2,
    closedHeightSlack: 0,
  };
  for (let index = 0; index < arguments_.length; index++) {
    const flag = arguments_[index];
    if (flag === '--help') return { help: true };
    if (flag === '--baseline') {
      options.baseline = true;
      continue;
    }
    const value = arguments_[++index];
    if (!value || value.startsWith('--')) throw new Error('Missing value for ' + flag);
    if (flag === '--evidence-dir') options.evidence = resolve(root, value);
    else if (flag === '--results-dir') options.results = resolve(root, value);
    else if (flag === '--mouth-crop') {
      const [width, height, x, y, ...extra] = value.split(':').map(Number);
      if (
        extra.length ||
        [width, height, x, y].some(
          // ROI 的像素边界必须精确，不能由 FFmpeg 自动截断小数或接受空字段。
          (number) => !Number.isInteger(number) || number < 0
        ) ||
        !width ||
        !height
      )
        throw new Error('Mouth crop must be width:height:x:y in integer pixels');
      options.crop = { width, height, x, y };
    } else if (flag === '--closed-baseline') {
      const [start, end, ...extra] = value.split(':').map(Number);
      if (
        extra.length ||
        !Number.isFinite(start) ||
        !Number.isFinite(end) ||
        start < 0 ||
        end <= start
      )
        throw new Error('Closed baseline must be start:end in recording PTS seconds');
      options.closedBaseline = { start, end };
    } else {
      const numericFlags = {
        '--minimum-row-pixels': 'minimumRowPixels',
        '--open-height-delta': 'openHeightDelta',
        '--closed-height-slack': 'closedHeightSlack',
      };
      if (!Object.hasOwn(numericFlags, flag)) throw new Error('Unknown option ' + flag);
      const number = Number(value);
      if (!Number.isInteger(number) || number < 0) throw new Error('Invalid integer for ' + flag);
      options[numericFlags[flag]] = number;
    }
  }
  if (!options.evidence || !options.crop)
    throw new Error('--evidence-dir and --mouth-crop are required');
  if (!options.minimumRowPixels || !options.openHeightDelta)
    throw new Error('Row support and opening height delta must be positive');
  if (options.closedHeightSlack >= options.openHeightDelta)
    throw new Error('Closed slack must be smaller than opening height delta');
  const historical = resolve(root, 'deployment/evidence/live2d-2026-10-06/browser-calibration');
  const distance = relative(historical, options.evidence);
  if (!distance || (!isAbsolute(distance) && distance.split(/[\\/]/)[0] !== '..'))
    throw new Error('Historical browser-calibration evidence must not be overwritten');
  return options;
}

/** 像素高度与嘴宽、闭唇厚度分开计算；每行需有多个浅色像素，抑制压缩产生的孤立亮点。 */
export function mouthPixelMetrics(pixels, width, height, minimumRowPixels = 3) {
  if (pixels.length !== width * height * 3)
    throw new Error('Mouth pixel buffer has wrong dimensions');
  const rowCounts = Array(height).fill(0);
  let count = 0;
  let minimumX = width;
  let maximumX = -1;
  let minimumY = height;
  let maximumY = -1;
  for (let pixel = 0; pixel < width * height; pixel++) {
    const point = pixel * 3;
    if (pixels[point] < 160 || pixels[point + 1] < 125 || pixels[point + 2] < 65) continue;
    const x = pixel % width;
    const y = Math.floor(pixel / width);
    count++;
    rowCounts[y]++;
    minimumX = Math.min(minimumX, x);
    maximumX = Math.max(maximumX, x);
    minimumY = Math.min(minimumY, y);
    maximumY = Math.max(maximumY, y);
  }
  const supportedRows = rowCounts.flatMap(
    // 高度需由有效嘴缘行支持，单像素高光不代表口腔变高。
    (pixelsInRow, y) => (pixelsInRow >= minimumRowPixels ? [y] : [])
  );
  return {
    creamPixels: count,
    outlineWidth: count ? maximumX - minimumX + 1 : 0,
    outlineHeight: count ? maximumY - minimumY + 1 : 0,
    supportedHeight: supportedRows.length ? supportedRows.at(-1) - supportedRows[0] + 1 : 0,
    supportedRows: supportedRows.length,
  };
}

/** 基准只取录像中的明确无声画面；闭唇已有浅色面积，不以软件口型或验收误差挑选阈值。 */
export function calibrateMouthDetector(video, activity, options) {
  const { start, end } = options.closedBaseline;
  const sounds = activity.filter(
    // 任一相交的音频窗口有声都禁止把该段作为闭嘴基准。
    (window) => window.start < end && window.end > start
  );
  const frames = video.filter(
    // 使用原录像 PTS，基准不依赖 report.trace 的 open 值。
    (frame) => frame.at >= start && frame.at < end
  );
  if (!sounds.length || sounds.some((window) => window.active))
    throw new Error('Closed-mouth baseline must contain recorded silence');
  if (frames.length < 5 || frames.some((frame) => !frame.supportedHeight))
    throw new Error('Closed-mouth baseline must contain at least five visible mouth frames');
  const heights = frames.map((frame) => frame.supportedHeight);
  const counts = frames.map((frame) => frame.creamPixels);
  const maximumClosedHeight = Math.max(...heights);
  return {
    kind: 'recorded-closed-mouth-height-baseline',
    ptsInterval: { start, end },
    minimumRowPixels: options.minimumRowPixels,
    baselineFrames: frames,
    baselineHeightP99: percentile(heights, 0.99),
    baselineHeightMaximum: maximumClosedHeight,
    baselineCreamPixelsMinimum: Math.min(...counts),
    baselineCreamPixelsMaximum: Math.max(...counts),
    openHeightAtLeast: maximumClosedHeight + options.openHeightDelta,
    closedHeightAtMost: maximumClosedHeight + options.closedHeightSlack,
    openHeightDelta: options.openHeightDelta,
    closedHeightSlack: options.closedHeightSlack,
  };
}

/** 两个阈值留出过渡带；嘴缘不可见属于未知，不能把 ROI 错位当作成功闭嘴。 */
export function classifyMouthFrame(frame, detector) {
  return {
    ...frame,
    mouthOpen: frame.supportedHeight >= detector.openHeightAtLeast,
    mouthClosed: frame.supportedHeight > 0 && frame.supportedHeight <= detector.closedHeightAtMost,
  };
}

/** 外部工具只读取合成录像及写证据，捕获诊断并限制输出，避免大逐帧 JSON 进入终端。 */
function execute(program, arguments_, binary = false) {
  const result = spawnSync(program, arguments_, {
    encoding: binary ? null : 'utf8',
    maxBuffer: 256 * 1024 * 1024,
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw result.error ?? new Error(`${program} failed: ${result.stderr}`);
  }
  const diagnostics = String(result.stderr).trim();
  for (const line of diagnostics.split(/\r?\n/).filter(Boolean)) {
    const normalized = line.replace(/ @ [0-9a-f]+/gi, '').replace(/: \d+ >= \d+$/, '');
    const warning = warnings.find(
      (entry) => entry.program === program && entry.message === normalized
    );
    if (warning) warning.count += 1;
    else warnings.push({ program, message: normalized, count: 1 });
  }
  return result.stdout;
}

/** 文件指纹将本次独立交叉验证绑定到原录像，不依赖文件名或旧报告日期。 */
function checksum(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/** 分析解码的录制声轨而非原 TTS 输入，音频和视频都沿原 WebM 的 PTS 读取。 */
async function readRecordingAudio() {
  const path = resolve(evidence, 'recording-audio.wav');
  execute('ffmpeg', [
    '-y',
    '-hide_banner',
    '-loglevel',
    'error',
    '-copyts',
    '-i',
    recording,
    '-map',
    '0:a:0',
    '-af',
    'aresample=24000:async=1:first_pts=0',
    '-ac',
    '1',
    '-c:a',
    'pcm_s16le',
    path,
  ]);
  const bytes = await readFile(path);
  let data;
  let rate;
  for (let offset = 12; offset + 8 <= bytes.length; ) {
    const size = bytes.readUInt32LE(offset + 4);
    const start = offset + 8;
    const id = bytes.toString('ascii', offset, offset + 4);
    if (start + size > bytes.length) throw new Error('Decoded WAV is truncated');
    if (id === 'fmt ') rate = bytes.readUInt32LE(start + 4);
    if (id === 'data') data = bytes.subarray(start, start + size);
    offset = start + size + (size % 2);
  }
  if (!data || !rate) throw new Error('Decoded recording has no PCM');
  const window = Math.round(rate * 0.01);
  const activity = [];
  for (let offset = 0; offset < data.length / 2; offset += window) {
    const end = Math.min(offset + window, data.length / 2);
    let squared = 0;
    for (let index = offset; index < end; index += 1) {
      const sample = data.readInt16LE(index * 2) / 32768;
      squared += sample * sample;
    }
    const rms = Math.sqrt(squared / (end - offset));
    activity.push({ start: offset / rate, end: end / rate, rms, active: rms > 0.012 });
  }
  return { rate, duration: data.length / 2 / rate, activity };
}

/** 仅嘴部小区域做科学像素检测，原始完整关键帧另存，检测不修改待验收画面。 */
function visualMouthFrames(crop, minimumRowPixels) {
  const timestamps = JSON.parse(
    execute('ffprobe', [
      '-v',
      'error',
      '-select_streams',
      'v:0',
      '-show_frames',
      '-show_entries',
      'frame=best_effort_timestamp_time',
      '-of',
      'json',
      recording,
    ])
  ).frames.map((frame) => Number(frame.best_effort_timestamp_time));
  const { width, height, x, y } = crop;
  const pixels = execute(
    'ffmpeg',
    [
      '-hide_banner',
      '-loglevel',
      'error',
      '-i',
      recording,
      '-map',
      '0:v:0',
      '-vf',
      `crop=${width}:${height}:${x}:${y}`,
      '-fps_mode',
      'passthrough',
      '-pix_fmt',
      'rgb24',
      '-enc_time_base:v',
      '1/1000',
      '-f',
      'rawvideo',
      'pipe:1',
    ],
    true
  );
  const frameBytes = width * height * 3;
  if (pixels.length !== timestamps.length * frameBytes) {
    throw new Error('Decoded frame count differs from original PTS');
  }
  return timestamps.map(
    // 帧号及 PTS 沿原始录制保存；像素检测不重采样、不插值或改写画面。
    (at, index) => ({
      index,
      at,
      ...mouthPixelMetrics(
        pixels.subarray(index * frameBytes, (index + 1) * frameBytes),
        width,
        height,
        minimumRowPixels
      ),
    })
  );
}

/** 最近秩保留全部可观测结果，缺失单独列出且阻止总体通过。 */
function percentile(values, fraction) {
  if (!values.length) return null;
  return [...values].sort((left, right) => left - right)[Math.ceil(values.length * fraction) - 1];
}

/** 稳定三个录制画面再判定闭嘴，避免把瞬时闭唇误认为一整句结束。 */
export function stableClosed(frames, start, end = Infinity) {
  for (let index = 0; index + 2 < frames.length; index += 1) {
    if (frames[index].at < start || frames[index + 2].at >= end) continue;
    if (
      frames.slice(index, index + 3).every(
        // 闭嘴以当前视频的可见闭唇基准判定，厚嘴缘仍可正常回到关闭状态。
        (frame) => frame.mouthClosed
      )
    ) {
      return frames[index];
    }
  }
  return null;
}

/** 报告时钟仅定位每条片段，实际误差完全使用同一录制文件的声音 PTS 与画面 PTS。 */
export function measureTrial(trial, activity, video) {
  const start = Math.max(0, trial.trace[0].audioAt - 0.15);
  const end = trial.trace.at(-1).audioAt + 0.15;
  const sounds = activity.filter(
    (window) => window.active && window.start >= start && window.end <= end
  );
  const frames = video.filter((frame) => frame.at >= start && frame.at < end);
  const onset = sounds[0]?.start ?? null;
  const stop = sounds.at(-1)?.end ?? null;
  const opened = frames.find((frame) => frame.mouthOpen) ?? null;
  const closed = stop === null ? null : stableClosed(frames, stop);
  return {
    id: trial.id,
    type: trial.type,
    recordedAudioOnset: onset,
    recordedAudioStop: stop,
    firstVisibleMouthOpen: opened?.at ?? null,
    firstStableMouthClosed: closed?.at ?? null,
    openingOffsetMs: onset === null || !opened ? null : (opened.at - onset) * 1000,
    closingDelayMs: stop === null || !closed ? null : (closed.at - stop) * 1000,
    visibleOpenFrame: opened,
    visibleCloseFrame: closed,
  };
}

/** 模型、官方源码及编译产物分别指纹，审查者能确认录制对应的实际资源和调度实现。 */
async function captureSourceFingerprints() {
  /** 按路径排序，不依赖 Windows 目录枚举顺序，聚合哈希可在下一次测量时逐项复核。 */
  async function listFiles(path) {
    const paths = [];
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const target = resolve(path, entry.name);
      if (entry.isDirectory()) paths.push(...(await listFiles(target)));
      else if (entry.isFile()) paths.push(target);
    }
    return paths.sort();
  }
  const groups = {
    model: await listFiles(resolve(root, 'web/public/avatar/xiaoya')),
    browserServedModel: await listFiles(
      resolve(root, '.tools/live2d-verification/public/avatar/xiaoya')
    ),
    browserServedSdkCore: await listFiles(
      resolve(root, '.tools/live2d-verification/public/avatar/vendor')
    ),
    sdk: [
      resolve(root, 'web/public/avatar/vendor/live2dcubismcore.min.js'),
      resolve(root, 'web/public/avatar/vendor/live2dcubismmotionsynccore.min.js'),
      ...(await listFiles(resolve(root, 'web/lib/avatar/vendor'))),
      ...(await listFiles(resolve(root, 'web/lib/avatar/vendor-source'))),
    ],
    runtime: [
      ...[
        'audio-bridge.ts',
        'behavior.ts',
        'lip-sync.ts',
        'live2d-runtime.ts',
        'motion-sync.ts',
        'sdk-loader.ts',
        'session-audio.tsx',
      ].map((name) => resolve(root, 'web/lib/avatar', name)),
      resolve(root, 'web/public/avatar/pcm-worklet.js'),
      resolve(root, 'web/components/app/avatar-stage.tsx'),
      resolve(root, 'web/tests/browser-avatar-harness.ts'),
      resolve(root, '.tools/live2d-verification/public/harness.js'),
      resolve(root, '.tools/live2d-verification/public/harness.js.map'),
      resolve(root, '.tools/live2d-verification/public/index.html'),
    ],
  };
  const result = { capturedAt: new Date().toISOString() };
  for (const [name, files] of Object.entries(groups)) {
    const entries = [];
    for (const path of files.sort()) {
      const bytes = await readFile(path);
      entries.push({
        path: relative(root, path).replaceAll('\\', '/'),
        bytes: bytes.length,
        sha256: checksum(bytes),
      });
    }
    result[name] = {
      fileCount: entries.length,
      aggregateSha256: checksum(JSON.stringify(entries)),
      files: entries,
    };
  }
  const servedEquivalence = [];
  const servedReferences = JSON.parse(
    await readFile(
      resolve(root, '.tools/live2d-verification/public/avatar/xiaoya/xiaoya.model3.json')
    )
  ).FileReferences;
  const runtimeModelReferences = new Set([
    'xiaoya/xiaoya.model3.json',
    ...[
      servedReferences.Moc,
      ...servedReferences.Textures,
      servedReferences.Physics,
      servedReferences.DisplayInfo,
      servedReferences.MotionSync,
      ...servedReferences.Expressions.map((expression) => expression.File),
      ...Object.values(servedReferences.Motions).flatMap((motions) =>
        motions.map((motion) => motion.File)
      ),
    ]
      .filter(Boolean)
      .map((file) => 'xiaoya/' + file),
  ]);
  for (const path of [...groups.browserServedModel, ...groups.browserServedSdkCore]) {
    const filename = relative(resolve(root, '.tools/live2d-verification/public/avatar'), path);
    const served = await readFile(path);
    let workspace;
    try {
      workspace = await readFile(resolve(root, 'web/public/avatar', filename));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      if (runtimeModelReferences.has(filename.replaceAll('\\', '/')))
        throw new Error('Actual recorded model references an absent workspace file');
      servedEquivalence.push({
        path: filename.replaceAll('\\', '/'),
        servedSha256: checksum(served),
        workspacePresent: false,
        reason:
          'Additional static inspection artifact, not a reference in the runtime model manifest',
      });
      continue;
    }
    const exactBytesEqual = served.equals(workspace);
    const parsedJsonEqual = filename.endsWith('.json')
      ? JSON.stringify(JSON.parse(served)) === JSON.stringify(JSON.parse(workspace))
      : null;
    servedEquivalence.push({
      path: filename.replaceAll('\\', '/'),
      servedSha256: checksum(served),
      workspaceSha256: checksum(workspace),
      exactBytesEqual,
      parsedJsonEqual,
    });
  }
  const sourceMap = JSON.parse(
    await readFile(resolve(root, '.tools/live2d-verification/public/harness.js.map'))
  );
  const bundledRuntimeSources = [];
  for (const name of [
    'audio-bridge',
    'behavior',
    'lip-sync',
    'live2d-runtime',
    'motion-sync',
    'sdk-loader',
  ]) {
    const suffix = '/web/lib/avatar/' + name + '.ts';
    const index = sourceMap.sources.findIndex((path) => path.endsWith(suffix));
    if (index < 0 || typeof sourceMap.sourcesContent[index] !== 'string')
      throw new Error('Runtime source missing from served bundle source map');
    const bundledSource = sourceMap.sourcesContent[index];
    const workspaceSource = await readFile(resolve(root, 'web/lib/avatar', name + '.ts'), 'utf8');
    /** TypeScript 同配置输出检查语义等价，只忽略注释及格式，不擅自排序有副作用的 import。 */
    const transpile = (source) =>
      ts.transpileModule(source, {
        fileName: name + '.ts',
        compilerOptions: {
          target: ts.ScriptTarget.ES2022,
          module: ts.ModuleKind.ESNext,
          removeComments: true,
          newLine: ts.NewLineKind.LineFeed,
        },
      }).outputText;
    bundledRuntimeSources.push({
      path: 'web/lib/avatar/' + name + '.ts',
      bundledSourceSha256: checksum(bundledSource),
      workspaceSourceSha256: checksum(workspaceSource),
      exactBytesEqual: bundledSource === workspaceSource,
      transpiledEqual: transpile(bundledSource) === transpile(workspaceSource),
    });
  }
  result.servedAssetsVersusWorkspace = servedEquivalence;
  result.bundledRuntimeSourcesVersusWorkspace = bundledRuntimeSources;
  await writeFile(resolve(evidence, 'source-fingerprints.json'), JSON.stringify(result, null, 2));
  return {
    capturedAt: result.capturedAt,
    model: result.model.aggregateSha256,
    browserServedModel: result.browserServedModel.aggregateSha256,
    browserServedSdkCore: result.browserServedSdkCore.aggregateSha256,
    sdk: result.sdk.aggregateSha256,
    runtime: result.runtime.aggregateSha256,
  };
}

/** 原录像单次解码提取所有关键帧，完整画面不重绘；帧号映射避免重复扫描长录像。 */
async function extractFrames(requests) {
  const unique = new Map();
  for (const { frame } of requests) if (frame) unique.set(frame.index, frame);
  const frames = [...unique.values()].sort((left, right) => left.index - right.index);
  if (!frames.length) return [];
  const directory = resolve(evidence, 'keyframes');
  await mkdir(directory);
  execute('ffmpeg', [
    '-y',
    '-hide_banner',
    '-loglevel',
    'error',
    '-i',
    recording,
    '-vf',
    `select='${frames.map((frame) => `eq(n,${frame.index})`).join('+')}'`,
    '-fps_mode',
    'passthrough',
    '-frames:v',
    String(frames.length),
    '-start_number',
    '0',
    resolve(directory, 'frame-%05d.png'),
  ]);
  const locations = new Map();
  for (const [index, frame] of frames.entries()) {
    const file = 'keyframes/frame-' + String(index).padStart(5, '0') + '.png';
    locations.set(frame.index, { file, sha256: checksum(await readFile(resolve(evidence, file))) });
  }
  return requests
    .filter((request) => request.frame)
    .map(
      // 同一原始帧可支持多条说明，复用文件但保留每条语音的独立来源和标签。
      ({ frame, ...labels }) => ({
        ...labels,
        ...locations.get(frame.index),
        ptsSeconds: frame.at,
        ...frame,
      })
    );
}

/** 每次分析只写新的证据目录；录制声音、可见开闭嘴与软件摘要分别保存，缺测不能通过。 */
async function main() {
  const options = parseOptions(process.argv.slice(2));
  if (options.help) {
    console.log(
      'Usage: node web/tests/analyze-avatar-recording.mjs --evidence-dir <new-directory> --mouth-crop <width:height:x:y> [--closed-baseline <start:end>] [--open-height-delta <pixels>] [--closed-height-slack <pixels>] [--minimum-row-pixels <count>] [--results-dir <directory>] [--baseline]'
    );
    return;
  }
  ({ results, baseline, evidence } = options);
  reportName = baseline ? 'report-before-fps-fix.json' : 'report.json';
  recordingName = baseline ? 'recording-before-fps-fix.webm' : 'recording.webm';
  recording = resolve(results, recordingName);
  await mkdir(evidence, { recursive: true });
  if ((await readdir(evidence)).length) throw new Error('Evidence directory must be empty');
  const reportBytes = await readFile(resolve(results, reportName));
  const report = JSON.parse(reportBytes);
  const sourceFingerprints = baseline ? null : await captureSourceFingerprints();
  const streamInfo = JSON.parse(
    execute('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', recording])
  );
  const audio = await readRecordingAudio();
  const rawVideo = visualMouthFrames(options.crop, options.minimumRowPixels);
  const detector = calibrateMouthDetector(rawVideo, audio.activity, options);
  const video = rawVideo.map((frame) => classifyMouthFrame(frame, detector));
  const cases = report.trials.map((trial) => measureTrial(trial, audio.activity, video));
  const onsets = cases
    .filter((trial) => trial.openingOffsetMs !== null)
    .map((trial) => Math.abs(trial.openingOffsetMs));
  const closes = cases
    .filter((trial) => trial.closingDelayMs !== null)
    .map((trial) => trial.closingDelayMs);
  const manifest = JSON.parse(
    await readFile(resolve(root, '.tools/live2d-verification/utterances/manifest.json'))
  );
  const pauseChecks = report.trials.flatMap((trial, index) => {
    const utterance = manifest.utterances.find((entry) => entry.id === trial.id);
    const offset = cases[index].recordedAudioOnset - trial.expectedStartAt;
    return trial.pauseChecks.map((pause, pauseIndex) => {
      const start = pause.startAt + offset;
      const end = start + utterance.inserted_silences[pauseIndex].duration_ms / 1000;
      const sounds = audio.activity.filter(
        (window) => window.start >= start + 0.15 && window.end < end - 0.02
      );
      const frames = video.filter((frame) => frame.at >= start + 0.15 && frame.at < end - 0.02);
      return {
        id: trial.id,
        recordedPauseStart: start,
        recordedPauseEnd: end,
        audioWindows: sounds.length,
        videoFrames: frames.length,
        maximumRms: Math.max(...sounds.map((window) => window.rms)),
        maximumCreamPixels: Math.max(...frames.map((frame) => frame.creamPixels)),
        maximumSupportedHeight: Math.max(...frames.map((frame) => frame.supportedHeight)),
        audioSilent: sounds.length > 0 && sounds.every((window) => !window.active),
        mouthClosed: frames.length > 0 && frames.every((frame) => frame.mouthClosed),
      };
    });
  });
  const requests = detector.baselineFrames
    .filter(
      (_, index, frames) =>
        index === 0 || index === Math.floor(frames.length / 2) || index === frames.length - 1
    )
    .map((frame) => ({ label: 'closed-baseline', frame: classifyMouthFrame(frame, detector) }));
  for (const trial of cases) {
    const activeFrames = video.filter(
      // 几何例图也独立选取，不再以软件 actualOpenAt 校准录像 wall clock。
      (frame) =>
        frame.mouthOpen &&
        frame.at >= trial.recordedAudioOnset &&
        frame.at < trial.recordedAudioStop
    );
    const peak = [...activeFrames].sort(
      (left, right) => right.supportedHeight - left.supportedHeight
    )[0];
    requests.push(
      {
        label: 'before-recorded-audio',
        trialId: trial.id,
        frame: video.filter((frame) => frame.at < trial.recordedAudioOnset).at(-1),
      },
      { label: 'first-visible-open', trialId: trial.id, frame: trial.visibleOpenFrame },
      { label: 'visible-open-peak', trialId: trial.id, frame: peak },
      { label: 'first-stable-close', trialId: trial.id, frame: trial.visibleCloseFrame }
    );
    if (trial.id === '03-short-wu')
      requests.push({
        label: 'round-mouth',
        trialId: trial.id,
        frame: [...activeFrames].sort(
          (left, right) =>
            right.supportedHeight / right.outlineWidth - left.supportedHeight / left.outlineWidth
        )[0],
      });
    if (trial.id === '02-short-yi')
      requests.push({
        label: 'wide-mouth',
        trialId: trial.id,
        frame: [...activeFrames].sort((left, right) => right.outlineWidth - left.outlineWidth)[0],
      });
  }
  for (const pause of pauseChecks)
    requests.push({
      label: 'pause-closed',
      trialId: pause.id,
      frame: video.find(
        (frame) => frame.at >= pause.recordedPauseStart + 0.25 && frame.at < pause.recordedPauseEnd
      ),
    });
  const keyframes = await extractFrames(requests);
  const result = {
    kind: 'independent-recording-cross-check',
    measurementEpoch: baseline ? 'baseline-before-fps-fix' : 'explicit-independent-output',
    syntheticAudio: true,
    humanDevicesVerified: false,
    physicalLoopbackRecorded: false,
    phonemeShapeHumanReviewed: false,
    description:
      'Recorded Canvas video PTS versus recorded synthetic Opus audio PTS; no physical speaker or microphone loopback.',
    fingerprints: {
      recordingSha256: checksum(await readFile(recording)),
      browserReportSha256: checksum(reportBytes),
      analyzerSha256: checksum(await readFile(fileURLToPath(import.meta.url))),
      sourceFingerprints,
    },
    recordingRelativePath: relative(root, recording).replaceAll('\\', '/'),
    streams: streamInfo.streams.map(
      ({ index, codec_name, codec_type, width, height, sample_rate, channels, start_time }) => ({
        index,
        codec: codec_name,
        type: codec_type,
        width,
        height,
        sampleRate: sample_rate,
        channels,
        startTime: start_time,
      })
    ),
    methodology: {
      audioWindowMs: 10,
      audioRmsThreshold: 0.012,
      mouthRegion: options.crop,
      creamPixelMinimumRgb: [160, 125, 65],
      detector,
      consecutiveClosedFrames: 3,
      limitation:
        'Pixel detector checks visible mouth opening only; phoneme correctness and actual output latency remain separate acceptance items.',
      independentOnset:
        'Recorded audio RMS and recorded video mouth height; trace only delimits trial windows, never determines onset or clock alignment.',
    },
    recordedDurationSeconds: audio.duration,
    decodedVideoFrames: video.length,
    summary: {
      utterances: cases.length,
      observedOnsets: onsets.length,
      observedCloses: closes.length,
      openingAbsoluteP95Ms: percentile(onsets, 0.95),
      openingAbsoluteMaxMs: Math.max(...onsets),
      closingDelayMaxMs: Math.max(...closes),
      pauseWindows: pauseChecks.length,
      allPausesAudiblySilent: pauseChecks.every((pause) => pause.audioSilent),
      allPausesVisiblyClosed: pauseChecks.every((pause) => pause.mouthClosed),
    },
    browserSoftwareSummary: report.summary,
    keyframes,
    unavailableKeyframes: requests
      .filter((request) => !request.frame)
      .map(({ label, trialId }) => ({ label, trialId })),
    warnings,
    pauseChecks,
    cases,
  };
  result.summary.complete = cases.length === 30 && onsets.length === 30 && closes.length === 30;
  result.summary.timingPassed =
    result.summary.complete &&
    result.summary.openingAbsoluteP95Ms <= 100 &&
    result.summary.closingDelayMaxMs <= 150;
  result.summary.passed =
    result.summary.timingPassed &&
    result.summary.allPausesAudiblySilent &&
    result.summary.allPausesVisiblyClosed;
  await copyFile(
    resolve(root, '.tools/live2d-verification/offline-motion-sync.json'),
    resolve(evidence, 'offline-motion-sync.json')
  );
  await copyFile(
    resolve(root, '.tools/live2d-verification/utterances/manifest.json'),
    resolve(evidence, 'utterance-manifest.json')
  );
  await writeFile(resolve(evidence, 'recording-cross-check.json'), JSON.stringify(result, null, 2));
  await writeFile(resolve(evidence, 'video-mouth-frames.json'), JSON.stringify(video));
  await writeFile(resolve(evidence, 'recorded-audio-windows.json'), JSON.stringify(audio.activity));
  await copyFile(resolve(results, reportName), resolve(evidence, 'browser-report.json'));
  await copyFile(recording, resolve(evidence, 'recording.webm'));
  console.log(
    JSON.stringify({
      ...result.summary,
      warnings,
      keyframes: keyframes.map(({ file, ptsSeconds }) => ({ file, ptsSeconds })),
    })
  );
  if (!result.summary.passed) process.exitCode = 1;
}

// 导入纯测量函数的测试不应启动 FFmpeg 或读取实际录制；CLI 才拥有证据写入职责。
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
