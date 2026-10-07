import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  calibrateMouthDetector,
  classifyMouthFrame,
  measureTrial,
  mouthPixelMetrics,
  parseOptions,
} from './analyze-avatar-recording.mjs';

/** 像素夹具只表示嘴缘与压缩孤点，不以软件嘴型值直接生成检测结果。 */
function createPixels(width, height, rows) {
  const pixels = Buffer.alloc(width * height * 3);
  for (const [y, count] of rows) {
    for (let x = 0; x < count; x++) pixels.set([245, 222, 164], (y * width + x) * 3);
  }
  return pixels;
}

/** 录像闭嘴已有较厚且连续的浅色笑弧，面积绝对阈值不能替代可见高度基准。 */
test('thick closed lip and compression highlights have distinct supported geometry', () => {
  const pixels = createPixels(64, 30, [
    [13, 24],
    [14, 24],
    [15, 24],
    [2, 1],
    [25, 1],
  ]);
  const metrics = mouthPixelMetrics(pixels, 64, 30, 3);
  assert.equal(metrics.creamPixels, 74);
  assert.equal(metrics.outlineHeight, 24);
  assert.equal(metrics.supportedHeight, 3);
  assert.equal(metrics.supportedRows, 3);
});

/** 相同厚唇从新录像静音区校准，宽度或面积变化不会在闭嘴时伪造 onset。 */
test('recorded silence calibrates closing height while opening needs added vertical extent', () => {
  const options = parseOptions([
    '--evidence-dir',
    '.tools/test-new-recording',
    '--mouth-crop',
    '64:30:205:249',
  ]);
  const frames = Array.from(
    { length: 8 },
    // 模拟压缩帧中一行的可见波动，阈值使用最大闭嘴高度而非理想零面积。
    (_, index) => ({
      at: 0.1 + index * 0.02,
      supportedHeight: index % 2 ? 3 : 4,
      creamPixels: 70 + index,
    })
  );
  const detector = calibrateMouthDetector(frames, [{ start: 0, end: 0.4, active: false }], options);
  assert.equal(detector.baselineHeightMaximum, 4);
  assert.equal(detector.openHeightAtLeast, 6);
  assert.equal(detector.closedHeightAtMost, 4);
  assert.equal(
    classifyMouthFrame({ supportedHeight: 4, creamPixels: 120 }, detector).mouthClosed,
    true
  );
  assert.equal(
    classifyMouthFrame({ supportedHeight: 4, creamPixels: 120 }, detector).mouthOpen,
    false
  );
  assert.equal(
    classifyMouthFrame({ supportedHeight: 6, creamPixels: 70 }, detector).mouthOpen,
    true
  );
  assert.equal(
    classifyMouthFrame({ supportedHeight: 0, creamPixels: 0 }, detector).mouthClosed,
    false
  );
});

/** 有声、错误 ROI 或帧数不足都不能生成貌似成功的闭嘴基准。 */
test('invalid closed-mouth calibration refuses silent-pass artifacts', () => {
  const options = parseOptions([
    '--evidence-dir',
    '.tools/test-new-recording',
    '--mouth-crop',
    '64:30:205:249',
  ]);
  const frames = Array.from(
    { length: 6 },
    // 仅固定基准帧几何，校准失败来自音频或 ROI 条件而非时序偶然。
    (_, index) => ({ at: 0.1 + index * 0.03, supportedHeight: 3, creamPixels: 60 })
  );
  assert.throws(
    // 独立录制音轨有声时禁止用该区间调低开口阈值。
    () => calibrateMouthDetector(frames, [{ start: 0, end: 0.4, active: true }], options),
    /recorded silence/
  );
  assert.throws(
    // ROI 无嘴时关闭状态必须为未知，不能因像素为零而通过。
    () =>
      calibrateMouthDetector(
        frames.map((frame) => ({ ...frame, supportedHeight: 0 })),
        [{ start: 0, end: 0.4, active: false }],
        options
      ),
    /visible mouth frames/
  );
});

/** 软件 actualOpenAt 故意错误，音频 PTS 与画面 PTS 仍独立给出 onset 和三帧闭嘴。 */
test('trial timings ignore software lip state and use recorded pixels and audio only', () => {
  const trial = {
    id: 'trial',
    type: 'short',
    actualOpenAt: 99,
    trace: [{ audioAt: 0 }, { audioAt: 1 }],
  };
  const video = [
    { at: 0.08, index: 0, mouthOpen: false, mouthClosed: true },
    { at: 0.14, index: 1, mouthOpen: true, mouthClosed: false },
    { at: 0.22, index: 2, mouthOpen: false, mouthClosed: true },
    { at: 0.24, index: 3, mouthOpen: true, mouthClosed: false },
    { at: 0.26, index: 4, mouthOpen: false, mouthClosed: true },
    { at: 0.28, index: 5, mouthOpen: false, mouthClosed: true },
    { at: 0.3, index: 6, mouthOpen: false, mouthClosed: true },
  ];
  const measured = measureTrial(trial, [{ start: 0.1, end: 0.2, active: true }], video);
  assert.equal(measured.recordedAudioOnset, 0.1);
  assert.equal(measured.firstVisibleMouthOpen, 0.14);
  assert.ok(Math.abs(measured.openingOffsetMs - 40) < 1e-9);
  assert.equal(measured.firstStableMouthClosed, 0.26);
  assert.ok(Math.abs(measured.closingDelayMs - 60) < 1e-9);
});

/** 新输出必须显式且独立，历史证据包括其子目录都不可被这次分析覆盖。 */
test('recording CLI requires new evidence destination and valid explicit ROI', () => {
  assert.throws(() => parseOptions([]), /required/);
  assert.throws(
    () =>
      parseOptions([
        '--evidence-dir',
        'deployment/evidence/live2d-2026-10-06/browser-calibration',
        '--mouth-crop',
        '64:30:205:249',
      ]),
    /Historical/
  );
  assert.throws(
    () =>
      parseOptions([
        '--evidence-dir',
        'deployment/evidence/live2d-2026-10-06/browser-calibration/new-run',
        '--mouth-crop',
        '64:30:205:249',
      ]),
    /Historical/
  );
  assert.throws(
    () =>
      parseOptions([
        '--evidence-dir',
        '.tools/test-new-recording',
        '--mouth-crop',
        '64:30:205.5:249',
      ]),
    /integer/
  );
  assert.throws(
    () =>
      parseOptions([
        '--evidence-dir',
        '.tools/test-new-recording',
        '--mouth-crop',
        '64:30:205:249',
        '--open-height-delta',
        '1',
        '--closed-height-slack',
        '1',
      ]),
    /smaller/
  );
});
