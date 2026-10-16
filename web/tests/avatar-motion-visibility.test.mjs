import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { AvatarPresentation, parseAvatarExpression } from '../lib/avatar/presentation.ts';

const directory = new URL('../public/avatar/xiaoya/', import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('xiaoya.model3.json', directory), 'utf8'));
const expressions = Object.fromEntries(
  manifest.FileReferences.Expressions.map((entry) => [
    entry.Name,
    parseAvatarExpression(JSON.parse(readFileSync(new URL(entry.File, directory), 'utf8'))),
  ])
);
let moc;
let model;
let neutral;
let headWidth;
let neutralArms;

/** 门槛来自当前官方19参数模型的真实几何能力，不能再把参数单位误报成可见角度。 */
before(async () => {
  const coreFile = new URL('../public/avatar/vendor/live2dcubismcore.min.js', import.meta.url);
  const context = {
    require: createRequire(coreFile),
    __dirname: fileURLToPath(new URL('.', coreFile)),
    __filename: fileURLToPath(coreFile),
    process,
    Buffer,
    console,
    WebAssembly,
    setTimeout,
    clearTimeout,
    TextDecoder,
    TextEncoder,
    ArrayBuffer,
    Uint8Array,
    Float32Array,
    Int32Array,
    Uint32Array,
    Int16Array,
    Uint16Array,
  };
  context.global = context;
  runInNewContext(readFileSync(coreFile, 'utf8'), context, { filename: fileURLToPath(coreFile) });
  await new Promise((resolve) => setTimeout(resolve, 50));
  const bytes = readFileSync(new URL(manifest.FileReferences.Moc, directory));
  assert.equal(
    createHash('sha256').update(bytes).digest('hex'),
    '601c6d70eab756fd2265973533a3315b0e7fc5440770a0effd2495c300bae0dc'
  );
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  moc = context.Live2DCubismCore.Moc.fromArrayBuffer(buffer);
  model = context.Live2DCubismCore.Model.fromMoc(moc);
  assert.equal(model.parameters.ids.length, 19);
  assert.ok(Array.from(model.parameters.types).every((type) => type === 0));
  neutral = head({});
  const xs = neutral.vertices.filter((_, index) => index % 2 === 0);
  headWidth = Math.max(...xs) - Math.min(...xs);
  neutralArms = Object.fromEntries(
    ['ArmL', 'ArmR'].map((id) => [
      id,
      Array.from(model.drawables.vertexPositions[model.drawables.ids.indexOf(id)]),
    ])
  );
});

/** 原生Core也必须释放，离线几何回归不留下WASM模型或影响后续测试。 */
after(() => {
  model?.release();
  moc?._release();
});

/** 重置并更新真正MOC，头部几何同时保留身摆贡献；坐标比例消除画布像素密度差异。 */
function head(values) {
  model.parameters.values.set(model.parameters.defaultValues);
  for (const [id, value] of Object.entries(values)) {
    const index = model.parameters.ids.indexOf(id);
    if (index >= 0) model.parameters.values[index] = value;
  }
  model.update();
  const vertices = Array.from(model.drawables.vertexPositions[model.drawables.ids.indexOf('Head')]);
  const center = [0, 1].map(
    (axis) =>
      vertices.filter((_, index) => index % 2 === axis).reduce((a, b) => a + b, 0) /
      (vertices.length / 2)
  );
  return { vertices, center };
}

/** 同索引网格刚性拟合将转角与平移分开，点头/摇头不冒称模型没有的三维角度。 */
function rotation(shape) {
  let dot = 0;
  let cross = 0;
  for (let index = 0; index < shape.vertices.length; index += 2) {
    const x = neutral.vertices[index] - neutral.center[0];
    const y = neutral.vertices[index + 1] - neutral.center[1];
    const u = shape.vertices[index] - shape.center[0];
    const v = shape.vertices[index + 1] - shape.center[1];
    dot += x * u + y * v;
    cross += x * v - y * u;
  }
  return (Math.atan2(cross, dot) * 180) / Math.PI;
}

/** 生产类拥有执行与回执；测试只改变公开输入，确保几何、许可和撤权不是手写动画公式的自证。 */
function trace(
  gesture,
  source,
  {
    energy = 0.45,
    fps = 60,
    start = 7,
    cancelAt = 2.3,
    style = gesture === 'tilt' ? 'curious' : 'neutral',
  } = {}
) {
  const runtime = new AvatarPresentation(expressions);
  const reference = new AvatarPresentation(expressions);
  const phase =
    source === 'command' ? 'thinking' : source === 'listening' ? 'listening' : 'speaking';
  const delivery = {
    instance: 'motion-visibility',
    id: 'cue',
    replyKey: 'reply',
    style,
    gesture,
    phase,
    turnId: 'turn',
    segmentKey: 'segment',
    gestureId: 'gesture',
    gestureSource: source === 'command' ? 'command' : 'dialogue',
    energy,
  };
  const rows = [];
  for (let frame = -2 * fps; frame <= 3 * fps; frame++) {
    const elapsed = frame / fps;
    const active = frame >= 0 && elapsed < cancelAt;
    const lip = active && phase === 'speaking' ? { open: 0.55, form: -0.2 } : null;
    const input = {
      time: start + elapsed,
      delta: 1 / fps,
      behavior: frame < 0 ? 'idle' : phase,
      gaze: { x: 0.225, y: 0.1 },
      blink: 1,
      lip,
    };
    const audio = {
      at: input.time,
      energy: lip?.open ?? 0,
      onset: frame === Math.round(fps * 0.15) ? 0.4 : 0,
    };
    const output = runtime.advance({ ...input, delivery: active ? { ...delivery, audio } : null });
    const baseline = reference.advance({
      ...input,
      delivery: active
        ? { ...delivery, audio, gesture: 'none', gestureId: '', gestureSource: 'none' }
        : null,
    });
    assert.equal(output.ParamMouthOpenY, lip?.open ?? 0);
    assert.equal(output.ParamMouthForm, lip?.form ?? 0);
    assert.ok(Object.values(output).every(Number.isFinite));
    const acknowledgments = runtime.drainAcknowledgments();
    if (frame >= 0) rows.push({ elapsed, output, baseline, acknowledgments });
  }
  return rows;
}

/** 读取原生手臂网格而不是Arm参数；按中性头宽归一，画布大小不应改变实际动作清晰度结论。 */
function armDisplacement(values, id = 'ArmL') {
  head(values);
  const actual = model.drawables.vertexPositions[model.drawables.ids.indexOf(id)];
  const original = neutralArms[id];
  let maximum = 0;
  for (let index = 0; index < actual.length; index += 2)
    maximum = Math.max(
      maximum,
      Math.hypot(actual[index] - original[index], actual[index + 1] - original[index + 1])
    );
  return maximum / headWidth;
}

/** 对照670fb14c正式原版实测MOC量，不镜像增益常量；提升必须真实出现在网格且倾听仍轻于说话、指令。 */
test('普通强度五动作的实际MOC清晰度提升，同时保留来源层次', () => {
  const before = {
    tilt: { dialogue: 2.6244054227, command: 5.8535718889, listening: 1.7534581392 },
    nod: { dialogue: 0.0093563152, command: 0.021909544, listening: 0.00657365 },
    shake: { dialogue: 0.0270301528, command: 0.0601802878, listening: 0.0180602604 },
    wave: { dialogue: 0.082960559, command: 0.1805237352, listening: 0.0561437189 },
    shy: { dialogue: 0.0247645202, command: 0.0549188074, listening: 0.0165131695 },
  };
  for (const gesture of ['tilt', 'nod', 'shake', 'wave', 'shy']) {
    const visible = {};
    for (const source of ['command', 'dialogue', 'listening']) {
      const rows = trace(gesture, source);
      if (gesture === 'tilt')
        visible[source] = Math.max(...rows.map((row) => Math.abs(rotation(head(row.output)))));
      else if (gesture === 'nod' || gesture === 'shake') {
        const axis = gesture === 'nod' ? 1 : 0;
        const values = rows.map((row) => head(row.output).center[axis]);
        visible[source] = (Math.max(...values) - Math.min(...values)) / headWidth;
      } else visible[source] = Math.max(...rows.map((row) => armDisplacement(row.output)));
      const minimumIncrease = gesture === 'wave' ? 1.12 : gesture === 'nod' ? 1.15 : 1.17;
      assert.ok(
        visible[source] >= before[gesture][source] * minimumIncrease,
        `${gesture}/${source}真实网格提升不足：${visible[source]}`
      );
      assert.ok(visible[source] < before[gesture][source] * 1.25);
      if (gesture === 'shy')
        for (const row of rows) {
          assert.equal(row.output.ParamArmL, row.output.ParamArmR);
          assert.ok(row.output.ParamArmL <= 0);
          assert.ok(Number.isFinite(armDisplacement(row.output, 'ArmR')));
        }
    }
    assert.ok(visible.listening < visible.dialogue && visible.dialogue < visible.command);
  }
});

/** 高能不能因退增益反而变小，官方范围和PCM嘴部独占同时检查；覆盖真实帧率而非只验证增益函数。 */
test('增幅后能量边界单调、七表情组合有限，嘴部与参数范围保持', () => {
  for (const gesture of ['tilt', 'nod', 'shake', 'wave', 'shy']) {
    const axis = {
      tilt: 'ParamAngleZ',
      nod: 'ParamAngleY',
      shake: 'ParamAngleX',
      wave: 'ParamArmL',
      shy: 'ParamArmL',
    }[gesture];
    for (const fps of [30, 60])
      for (const source of ['command', 'dialogue', 'listening']) {
        let previousPeak = 0;
        for (const energy of [0, 0.25, 0.45, 0.5, 0.6, 0.75, 0.9, 1]) {
          const rows = trace(gesture, source, { energy, fps });
          const peak = Math.max(...rows.map((row) => Math.abs(row.output[axis])));
          assert.ok(peak > previousPeak, `${gesture}/${source}/${fps}/${energy}不能反向削弱`);
          previousPeak = peak;
          for (const row of rows) {
            for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX'])
              assert.ok(Math.abs(row.output[id]) <= 30);
            assert.ok(Math.abs(row.output.ParamArmL) < 1);
            assert.ok(Math.abs(row.output.ParamArmR) < 1);
          }
        }
      }
    for (const style of ['neutral', 'happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised'])
      for (const row of trace(gesture, 'command', { energy: 1, style })) {
        assert.ok(Object.values(row.output).every(Number.isFinite));
        assert.ok(head(row.output).vertices.every(Number.isFinite));
      }
  }
});

/** 放大后取消仅留视觉缓出；700ms内网格回到背景，终态仍canceled且不能复活或把嘴部惯性当收尾。 */
test('较大姿态在发力位置撤权保持原步长、700ms收回和真实取消回执', () => {
  for (const gesture of ['tilt', 'nod', 'shake', 'wave', 'shy'])
    for (const fps of [30, 60])
      for (const cancelAt of [0.2, 0.35, 0.5, 0.6, 0.75, 0.8, 1]) {
        const rows = trace(gesture, 'dialogue', { fps, cancelAt });
        const acknowledgments = rows.flatMap((row) => row.acknowledgments);
        assert.equal(acknowledgments.filter((ack) => ack.status === 'started').length, 1);
        assert.equal(acknowledgments.filter((ack) => ack.status === 'canceled').length, 1);
        assert.ok(acknowledgments.every((ack) => ack.status !== 'completed'));
        for (let index = 1; index < rows.length; index++) {
          const row = rows[index];
          for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX'])
            assert.ok(
              Math.abs(row.output[id] - rows[index - 1].output[id]) < (fps === 30 ? 3 : 2),
              `${gesture}/${fps}/${cancelAt}/${row.elapsed}/${id}`
            );
          if (row.elapsed >= cancelAt) assert.equal(row.output.ParamMouthOpenY, 0);
        }
        const returned = rows.find((row) => row.elapsed >= cancelAt + 0.7);
        const actual = head(returned.output);
        const background = head(returned.baseline);
        assert.ok(
          actual.vertices.every(
            (value, index) => Math.abs(value - background.vertices[index]) < 1e-4
          )
        );
        assert.ok(Math.abs(returned.output.ParamArmL) < 0.00003);
        assert.ok(Math.abs(returned.output.ParamArmR) < 0.00003);
      }
});

/** 新增可见几何门只补充原数值门，绝不以肉眼或几何通过放宽原五动作协议幅度。 */
function originalGate(gesture, rows, command) {
  const range = (id) => {
    const values = rows.map((row) => row.output[id]);
    return {
      min: Math.min(...values),
      max: Math.max(...values),
      span: Math.max(...values) - Math.min(...values),
    };
  };
  const x = range('ParamAngleX');
  const y = range('ParamAngleY');
  const z = range('ParamAngleZ');
  if (gesture === 'tilt') return z.max > (command ? 8 : 4.5) && z.span > 2.5;
  if (gesture === 'nod') return y.min < -2 && y.span > (command ? 8 : 4);
  return x.min < -2 && x.max > (command ? 8 : 4) && x.span > (command ? 12 : 5);
}

/** 原19真正started后只剩425.8ms；仅量许可内60Hz真实网格，取消后的惯性和后句音频不能补足幅度。 */
test('短音频自然点头从真实started起425.8ms内达到原MOC门，撤权闭嘴且不复活', (t) => {
  const runtime = new AvatarPresentation(expressions);
  const reference = new AvatarPresentation(expressions);
  const cue = {
    instance: 'short-nod',
    id: 'first-segment',
    replyKey: 'reply',
    style: 'neutral',
    gesture: 'nod',
    phase: 'speaking',
    turnId: 'turn',
    segmentKey: 'segment',
    gestureId: 'segment:nod',
    gestureSource: 'dialogue',
    energy: 0.5,
  };
  let startedAt = null;
  let revoked = false;
  let previous = -2 - 1 / 60;
  const acknowledgments = [];
  const centers = [];
  const tails = [];

  /** 撤权是精确时间事件而非额外渲染样本；它只关口型与许可，几何证据仍来自原60Hz帧。 */
  function step(elapsed, boundaryEvent = false) {
    const active = elapsed >= 0 && !revoked;
    const lip = active ? { open: 0.55, form: -0.2 } : null;
    const audio = {
      at: 7 + elapsed,
      energy: active ? 0.55 : 0,
      onset: Math.abs(elapsed - 0.15) < 1e-9 ? 0.4 : 0,
    };
    const input = {
      time: 7 + elapsed,
      delta: elapsed - previous,
      behavior: elapsed < 0 ? 'idle' : 'speaking',
      gaze: { x: 0.225, y: 0.1 },
      blink: 1,
      lip,
    };
    const delivery = active || elapsed >= 1.5 ? { ...cue, audio } : null;
    const output = runtime.advance({ ...input, delivery });
    const baseline = reference.advance({
      ...input,
      delivery: delivery
        ? { ...delivery, gesture: 'none', gestureId: '', gestureSource: 'none' }
        : null,
    });
    for (const ack of runtime.drainAcknowledgments()) {
      acknowledgments.push({ ...ack, elapsed });
      if (ack.status === 'started') {
        assert.equal(startedAt, null, '同一已消费身份不得第二次开始');
        assert.equal(ack.stroke_started, true);
        startedAt = elapsed;
      }
    }
    if (!boundaryEvent && startedAt !== null && !revoked) {
      assert.ok(elapsed - startedAt < 0.4258);
      centers.push(head(output).center[1]);
    }
    if (revoked) {
      assert.equal(output.ParamMouthOpenY, 0);
      assert.equal(output.ParamMouthForm, 0);
      if (elapsed >= 1.5) tails.push({ output, baseline });
    }
    previous = elapsed;
  }

  for (let frame = -120; frame <= 180; frame++) {
    const elapsed = frame / 60;
    if (!revoked && startedAt !== null && elapsed >= startedAt + 0.4258) {
      revoked = true;
      step(startedAt + 0.4258, true);
    }
    step(elapsed);
  }
  assert.ok(startedAt !== null, '必须由生产类真实stroke回执建立观察起点');
  assert.ok(centers.length >= 20);
  const fraction = (Math.max(...centers) - Math.min(...centers)) / headWidth;
  t.diagnostic(
    JSON.stringify({
      startedAt,
      pcmPermissionMs: 425.8,
      geometryFrameCount: centers.length,
      headYRangeFractionWidth: fraction,
      threshold: 0.009,
    })
  );
  assert.ok(fraction >= 0.009, `许可内实际头部纵幅/头宽不足：${fraction}`);
  assert.deepEqual(
    acknowledgments.map((ack) => ack.status),
    ['started', 'canceled']
  );
  assert.equal(acknowledgments[1].stroke_started, true);
  assert.equal(acknowledgments[1].elapsed, startedAt + 0.4258);
  assert.ok(tails.length > 30);
  assert.ok(
    tails.every(({ output, baseline }) =>
      ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX'].every(
        (id) => Math.abs(output[id] - baseline[id]) < 1e-6
      )
    )
  );
});

for (const gesture of ['tilt', 'nod', 'shake']) {
  for (const source of ['command', 'dialogue']) {
    /** 同一真实模型跨帧率/待机相位过固定可见门，防止只挑一个背景角度或参数标签作为通过证据。 */
    test(`${source}/${gesture}：原数值门与额外真实头部可见幅度同时通过`, () => {
      for (const fps of [30, 60]) {
        for (const start of [7, 12, 19]) {
          const rows = trace(gesture, source, { fps, start });
          assert.ok(originalGate(gesture, rows, source === 'command'));
          const shapes = rows.map((row) => head(row.output));
          if (gesture === 'tilt') {
            const maximum = Math.max(...shapes.map((shape) => Math.abs(rotation(shape))));
            assert.ok(maximum >= (source === 'command' ? 5 : 2.3), `实际转角不足：${maximum}`);
          } else {
            const axis = gesture === 'nod' ? 1 : 0;
            const values = shapes.map((shape) => shape.center[axis]);
            const widthFraction = (Math.max(...values) - Math.min(...values)) / headWidth;
            const minimum =
              gesture === 'nod'
                ? source === 'command'
                  ? 0.02
                  : 0.009
                : source === 'command'
                  ? 0.04
                  : 0.02;
            assert.ok(widthFraction >= minimum, `实际头部位移/头宽不足：${widthFraction}`);
          }
        }
      }
    });
  }
}

/** 高强度可触碰原夹界但不能长时间卡住；界限取现有准备窗/回收节奏，不增加能量下限或新循环。 */
test('0/.45/1边界有限、原门保持、夹界停留有界且零步长不跳姿态', () => {
  for (const gesture of ['tilt', 'nod', 'shake']) {
    const axis =
      gesture === 'tilt' ? 'ParamAngleZ' : gesture === 'nod' ? 'ParamAngleY' : 'ParamAngleX';
    for (const source of ['command', 'dialogue', 'listening']) {
      for (const energy of [0, 0.45, 1]) {
        for (const fps of [30, 60]) {
          const rows = trace(gesture, source, { energy, fps });
          assert.ok(rows.every((row) => Math.abs(row.output[axis]) <= 30));
          if (source === 'command') assert.ok(originalGate(gesture, rows, true));
          const near = rows.filter((row) => Math.abs(row.output[axis]) >= 29).length / fps;
          let longest = 0;
          let run = 0;
          rows.slice(1).forEach((row, index) => {
            run =
              Math.abs(row.output[axis]) >= 29 &&
              Math.abs(row.output[axis] - rows[index].output[axis]) < 0.03
                ? run + 1 / fps
                : 0;
            longest = Math.max(longest, run);
          });
          assert.ok(near <= 0.3 && longest <= 0.12, `${gesture}/${energy}不得出现异常长平顶`);
        }
      }
    }
  }
  const runtime = new AvatarPresentation(expressions);
  const state = {
    time: 7,
    delta: 1 / 60,
    behavior: 'thinking',
    delivery: null,
    gaze: { x: 0, y: 0 },
    blink: 1,
    lip: null,
  };
  const first = runtime.advance(state);
  assert.deepEqual(runtime.advance({ ...state, time: 8, delta: 0 }), first);
});

/** 指令不靠PCM启动，倾听保持原三成而说话保持四成五；幅度校准不得把轻反应升格为指令动作。 */
test('原command/dialogue/listening幅度关系与PCM独占保持', () => {
  for (const gesture of ['tilt', 'nod', 'shake']) {
    const axis =
      gesture === 'tilt' ? 'ParamAngleZ' : gesture === 'nod' ? 'ParamAngleY' : 'ParamAngleX';
    const peak = (source) =>
      Math.max(...trace(gesture, source).map((row) => Math.abs(row.output[axis])));
    const command = peak('command');
    assert.ok(Math.abs(peak('dialogue') / command - 0.45) < 0.015);
    assert.ok(Math.abs(peak('listening') / command - 0.3) < 0.015);
  }
});

/** 在发力中撤权仍沿原阻尼回到背景，增大幅度不能留下旧候选、口型或动作复活。 */
test('三轴发力中取消收敛且不复活，Jev眉眼独立于动作标量', () => {
  for (const gesture of ['tilt', 'nod', 'shake']) {
    for (const source of ['command', 'dialogue', 'listening']) {
      const rows = trace(gesture, source, { energy: 1, cancelAt: 0.6 });
      const tail = rows.filter((row) => row.elapsed >= 1.5);
      assert.ok(tail.length > 20);
      assert.ok(
        tail.every((row) =>
          ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX'].every(
            (id) => Math.abs(row.output[id] - row.baseline[id]) < 1e-5
          )
        )
      );
      assert.ok(
        tail.every((row) => row.output.ParamMouthOpenY === 0 && row.output.ParamMouthForm === 0)
      );
      if (gesture === 'tilt') {
        const active = rows.find((row) => row.elapsed >= 0.5);
        assert.ok(active.output.ParamBrowLY > 0.65 && active.output.ParamBrowLAngle > 0.34);
      }
    }
  }
});

/** 原.6秒撤权遗漏了歪头峰后回收尖峰；增加峰前/峰后时刻仍沿用原典型强度3/2步长门。 */
test('自然主轴在多个发力位置撤权仍平滑归还，口型当帧关闭', () => {
  for (const gesture of ['tilt', 'nod', 'shake']) {
    for (const fps of [30, 60]) {
      for (const cancelAt of [0.4, 0.6, 0.75, 0.8, 1]) {
        const rows = trace(gesture, 'dialogue', { fps, cancelAt });
        for (let index = 1; index < rows.length; index++) {
          const row = rows[index];
          for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ']) {
            const step = Math.abs(row.output[id] - rows[index - 1].output[id]);
            assert.ok(step < (fps === 30 ? 3 : 2), `${gesture}/${fps}/${cancelAt}/${id}: ${step}`);
          }
          if (row.elapsed >= cancelAt) {
            assert.equal(row.output.ParamMouthOpenY, 0);
            assert.equal(row.output.ParamMouthForm, 0);
          }
        }
        const last = rows.at(-1);
        for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX'])
          assert.ok(Math.abs(last.output[id] - last.baseline[id]) < 1e-6);
      }
    }
  }
});
