import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import {
  AvatarPresentation,
  isAvatarExpressionName,
  parseAvatarExpression,
} from '../lib/avatar/presentation.ts';

/** 普通思考以微动收敛且帧率无关；只测生产阻尼，不用独立easing副本掩盖阶段切换的额外动作。 */
test('真实 presentation 在 30／60 fps 下保持同速，后台首步有界且不越界', () => {
  const slow = new AvatarPresentation();
  const fast = new AvatarPresentation();
  let at30;
  let at60;
  for (let frame = 0; frame < 30; frame++)
    at30 = slow.advance(input({ behavior: 'thinking', delta: 1 / 30 }));
  for (let frame = 0; frame < 60; frame++)
    at60 = fast.advance(input({ behavior: 'thinking', delta: 1 / 60 }));
  assert.ok(Math.abs(at30.ParamAngleX - at60.ParamAngleX) < 1e-12);
  assert.ok(at30.ParamAngleX > 1.99 && at30.ParamAngleX < 2);
  const paused = new AvatarPresentation();
  const first = paused.advance(input({ behavior: 'confused', delta: 600 }));
  assert.ok(first.ParamAngleZ > -7 && first.ParamAngleZ < 0);
  assert.equal(
    paused.advance(input({ behavior: 'confused', delta: 0 })).ParamAngleZ,
    first.ParamAngleZ
  );
});

/** 缩小普通头幅后仍保留同向轻身摆，眼球独立全幅；不通过放大背景姿态制造像手势的联动。 */
test('头部转向带动同向小幅身摆，视线与音频口型仍各自拥有参数', () => {
  const right = settle(new AvatarPresentation(), { gaze: { x: 1, y: 0.4 } });
  const left = settle(new AvatarPresentation(), { gaze: { x: -1, y: -0.4 } });
  assert.ok(right.ParamBodyAngleX > 0.15 && right.ParamBodyAngleX < right.ParamAngleX);
  assert.ok(left.ParamBodyAngleX < -0.15 && left.ParamBodyAngleX > left.ParamAngleX);
  assert.ok(Math.abs(right.ParamEyeBallX - 1) < 1e-12);
  assert.ok(Math.abs(left.ParamEyeBallX + 1) < 1e-12);
  assert.ok(Math.abs(right.ParamEyeBallY - 0.4) < 1e-12);
  assert.ok(Math.abs(left.ParamEyeBallY + 0.4) < 1e-12);
  assert.equal(right.ParamMouthOpenY, 0);
  assert.equal(left.ParamMouthOpenY, 0);
});

/** 零时刻不预走不同长度的一帧；严格对齐物理时间后保留原误差门，避免放大动作揭露的是采样偏移。 */
test('身摆随真实摇头在 30／60fps 下连贯，取消后与中性姿态收敛', () => {
  const slow = new AvatarPresentation();
  const fast = new AvatarPresentation();
  const neutral = new AvatarPresentation();
  let low = Infinity;
  let high = -Infinity;
  let cancelled;
  let resting;
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60;
    const delivery = time < 0.8 ? cue('body-shake', 'shake') : null;
    const state = input({
      time,
      delta: frame === 0 ? 0 : 1 / 60,
      delivery,
      lip: delivery ? { open: 0.6, form: 0 } : null,
    });
    const at60 = fast.advance(state);
    resting = neutral.advance(input({ time }));
    if (delivery) {
      low = Math.min(low, at60.ParamBodyAngleX);
      high = Math.max(high, at60.ParamBodyAngleX);
    } else {
      assert.equal(at60.ParamMouthOpenY, 0);
      cancelled = at60;
    }
    if (frame % 2 === 0) {
      const at30 = slow.advance({ ...state, delta: frame === 0 ? 0 : 1 / 30 });
      assert.ok(Math.abs(at30.ParamBodyAngleX - at60.ParamBodyAngleX) < 0.7);
      assert.ok(Math.abs(at30.ParamBodyAngleX) < 6);
    }
  }
  assert.ok(low < -0.2 && high > 0.2, '身摆应随摇头左右往返');
  assert.ok(Math.abs(cancelled.ParamBodyAngleX - resting.ParamBodyAngleX) < 1e-6);
  assert.ok(Math.abs(cancelled.ParamArmL) < 1e-6);
  assert.ok(Math.abs(cancelled.ParamArmR) < 1e-6);
});

/** 固定时间、眨眼和视线，把模型表达与浏览器调度/随机微动解耦。 */
function input(overrides = {}) {
  return {
    time: 0,
    delta: 1 / 60,
    behavior: 'speaking',
    delivery: null,
    gaze: { x: 0, y: 0 },
    blink: 1,
    lip: null,
    ...overrides,
  };
}

/** 测试通过正式资源解析器构建表情，防止合成测试绕过配置安全约束。 */
function expression(parameters, overrides = {}) {
  return parseAvatarExpression({
    Type: 'Live2D Expression',
    FadeInTime: 0.3,
    FadeOutTime: 0.35,
    Parameters: parameters,
    ...overrides,
  });
}

/** 重复帧保持同一时钟用于收敛断言，不会意外让手势计时改变被测表情。 */
function settle(presentation, overrides = {}, frames = 180) {
  let output;
  for (let frame = 0; frame < frames; frame++) output = presentation.advance(input(overrides));
  return output;
}

/** 测试显式区分回复与句段身份，不用段 ID 或旧版缺省字段模拟跨回复语义。 */
function cue(id, gesture = 'nod', reply = 'reply-a', style = 'neutral') {
  return {
    id,
    instance: 'job-a',
    replyKey: JSON.stringify(['job-a', reply]),
    gesture,
    style,
    phase: 'speaking',
    turnId: reply,
    segmentKey: id,
    gestureId: gesture === 'none' ? '' : id,
    gestureSource: gesture === 'none' ? 'none' : 'command',
    energy: 1,
  };
}

/** 用真实失败的低强度和不同时钟预热待机，避免只从零姿态测试而漏掉反向基姿态抵消。 */
function naturalMotionTrace(
  gesture,
  { startTime = 7, fps = 60, energy = 0.45, gazeX = 0.225, cancelAt = Infinity } = {}
) {
  const { expressions } = resourceExpressions();
  const runtime = new AvatarPresentation(expressions);
  const control = new AvatarPresentation(expressions);
  const style = gesture === 'tilt' ? 'curious' : 'neutral';
  const delivery = {
    ...cue(`natural-${gesture}`, gesture, 'natural-turn', style),
    gestureSource: 'dialogue',
    energy,
  };
  const reference = {
    ...delivery,
    id: `control-${gesture}`,
    gesture: 'none',
    gestureId: '',
    gestureSource: 'none',
  };
  const rows = [];
  let previous;
  for (let frame = -2 * fps; frame <= 3 * fps; frame++) {
    const time = startTime + frame / fps;
    const elapsed = frame / fps;
    const active = frame >= 0 && elapsed < cancelAt;
    const lip = active && elapsed < 2 ? { open: 0.55, form: 0.2 } : null;
    const audio = {
      at: time,
      energy: lip?.open ?? 0,
      onset: active && [Math.round(fps * 0.15), Math.round(fps * 0.65)].includes(frame) ? 0.4 : 0,
    };
    const state = input({
      time,
      delta: 1 / fps,
      behavior: frame < 0 ? 'idle' : 'speaking',
      gaze: { x: gazeX, y: 0.1 },
      lip,
    });
    const output = runtime.advance({ ...state, delivery: active ? { ...delivery, audio } : null });
    const baseline = control.advance({
      ...state,
      delivery: active ? { ...reference, audio } : null,
    });
    if (frame >= 0) rows.push({ elapsed, output, baseline, previous, lip });
    previous = output;
  }
  return rows;
}

/** 直接读取当前 manifest 的资源，避免手写测试表情与浏览器实际表现逐渐分叉。 */
function resourceExpressions() {
  const directory = new URL('../public/avatar/xiaoya/', import.meta.url);
  const manifest = JSON.parse(readFileSync(new URL('xiaoya.model3.json', directory), 'utf8'));
  const expressions = {};
  for (const reference of manifest.FileReferences.Expressions) {
    expressions[reference.Name] = parseAvatarExpression(
      JSON.parse(readFileSync(new URL(reference.File, directory), 'utf8'))
    );
  }
  return { directory, manifest, expressions };
}

/** 只使用随项目交付的官方 Core 与 manifest 指向的 MOC，不接浏览器、网络或候选模型。 */
async function productionCoreModel(directory, manifest) {
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
  // Core 的 WASM 实例先完成异步初始化；不以等待代替后面的实际载入和一致性断言。
  await new Promise((resolve) => setTimeout(resolve, 50));
  const bytes = readFileSync(new URL(manifest.FileReferences.Moc, directory));
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const moc = context.Live2DCubismCore.Moc.fromArrayBuffer(buffer);
  assert.ok(moc);
  assert.equal(moc.hasMocConsistency(buffer), 1);
  return { moc, model: context.Live2DCubismCore.Model.fromMoc(moc) };
}

/** 重置参数并读取所有部件；统一到宽度2的画布单位，保留旧门对应的实际像素幅度而不受PPU改变影响。 */
function modelVertices(model, values) {
  model.parameters.values.set(model.parameters.defaultValues);
  for (const [name, value] of Object.entries(values)) {
    const index = model.parameters.ids.indexOf(name);
    if (index >= 0) model.parameters.values[index] = value;
  }
  model.update();
  const scale =
    (2 * model.canvasinfo.PixelsPerUnit) /
    Math.max(model.canvasinfo.CanvasWidth, model.canvasinfo.CanvasHeight);
  const vertices = [];
  for (const positions of model.drawables.vertexPositions) {
    for (const value of positions) vertices.push(value * scale);
  }
  return Float32Array.from(vertices);
}

/** 每个分层网格保留自己的索引范围，合并后仍检查全部三角；不假设嘴部第0网格就是整个人物。 */
function modelTopology(model) {
  const indices = [];
  const drawables = new Map();
  let offset = 0;
  for (let drawable = 0; drawable < model.drawables.count; drawable++) {
    const id = model.drawables.ids[drawable];
    const count = model.drawables.vertexCounts[drawable];
    assert.equal(drawables.has(id), false, '真实分层网格身份不能重复');
    assert.equal(model.drawables.vertexPositions[drawable].length, count * 2);
    drawables.set(id, { offset, count });
    const localIndices = model.drawables.indices[drawable];
    assert.equal(localIndices.length % 3, 0, `${id}: 三角索引必须完整`);
    for (const index of localIndices) {
      assert.ok(index >= 0 && index < count, `${id}: 三角不能借用其他部件顶点`);
      indices.push(offset + index);
    }
    offset += count;
  }
  return { indices, drawables };
}

/** 用独立手掌的整体中心判断内收，避免继承旧整图像素坐标后读到眼睛或不存在的顶点。 */
function drawableCenterX(vertices, drawable) {
  assert.ok(drawable && drawable.count > 0, '内收判断必须绑定真实手掌');
  let total = 0;
  for (let index = drawable.offset; index < drawable.offset + drawable.count; index++) {
    total += vertices[index * 2];
  }
  return total / drawable.count;
}

/** 真实网格差值用于验证参数已经绑定；不将几何变化本身宣称为视觉或语义验收。 */
function maximumDelta(left, right) {
  let maximum = 0;
  for (let index = 0; index < left.length; index += 2) {
    maximum = Math.max(
      maximum,
      Math.hypot(left[index] - right[index], left[index + 1] - right[index + 1])
    );
  }
  return maximum;
}

/** 全网格绕序包含透明边缘，防止闭眼或开口组合产生截图难以察觉的局部翻折。 */
function triangleArea(vertices, indices, index) {
  const a = indices[index] * 2;
  const b = indices[index + 1] * 2;
  const c = indices[index + 2] * 2;
  return (
    (vertices[b] - vertices[a]) * (vertices[c + 1] - vertices[a + 1]) -
    (vertices[c] - vertices[a]) * (vertices[b + 1] - vertices[a + 1])
  );
}

/** Add 必须相对基准而不是前一帧，长句中的眉毛不能一路漂移到极限。 */
test('表情资源真实应用但数百帧不累计参数', () => {
  const runtime = new AvatarPresentation({
    happy: expression([{ Id: 'ParamBrowLY', Value: 0.3, Blend: 'Add' }]),
  });
  const delivery = cue('speech-1', 'none', 'reply-a', 'happy');
  const output = settle(runtime, { delivery }, 600);
  assert.ok(Math.abs(output.ParamBrowLY - 0.3) < 1e-6);
  assert.equal(output.ParamBrowRY, 0);
  const recovered = settle(runtime, { behavior: 'idle' });
  assert.ok(recovered.ParamBrowLY < 1e-6);
});

/** 表情、动作和物理不得成为第二个嘴部作者；闭眼遮罩也必须压过表情眼睛开度。 */
test('拒绝嘴部和肢体表情，音频口型独占且眨眼不被表情撑开', () => {
  for (const id of [
    'ParamMouthOpenY',
    'ParamMouthForm',
    'ParamArmL',
    'ParamBreath',
    'ParamLeafSwing',
  ]) {
    assert.throws(() => expression([{ Id: id, Value: 0.5, Blend: 'Overwrite' }]), /参数无效/);
  }
  const runtime = new AvatarPresentation({
    happy: expression([
      { Id: 'ParamEyeLOpen', Value: 0.8, Blend: 'Multiply' },
      { Id: 'ParamEyeSmile', Value: 1, Blend: 'Overwrite' },
    ]),
  });
  const output = settle(runtime, {
    delivery: cue('speech-1', 'wave', 'reply-a', 'happy'),
    blink: 0,
    lip: { open: 0.63, form: -0.4 },
  });
  assert.equal(output.ParamEyeLOpen, 0);
  assert.equal(output.ParamEyeROpen, 0);
  assert.ok(output.ParamEyeSmile > 0.99);
  assert.equal(output.ParamMouthOpenY, 0.63);
  assert.equal(output.ParamMouthForm, -0.4);
  const silent = runtime.advance(input());
  assert.equal(silent.ParamMouthOpenY, 0);
  assert.equal(silent.ParamMouthForm, 0);
});

/** 跨表情淡化从同一基准求差，资源顺序不得使 Multiply/Overwrite 产生不同动作。 */
test('交叉淡化与表情加载顺序无关', () => {
  const happy = expression([{ Id: 'ParamEyeLOpen', Value: 0.5, Blend: 'Multiply' }]);
  const concerned = expression([{ Id: 'ParamEyeLOpen', Value: 0.9, Blend: 'Overwrite' }]);
  const left = new AvatarPresentation({ happy, concerned });
  const right = new AvatarPresentation({ concerned, happy });
  for (const style of ['happy', 'concerned', 'happy', 'neutral']) {
    for (let frame = 0; frame < 12; frame++) {
      const state = input({ delivery: cue(style, 'none', 'reply-a', style) });
      assert.deepEqual(left.advance(state), right.advance(state));
    }
  }
});

/** 同一段动作身份固定，视觉版本与强度更新不能重播或修改已经开始的主动作。 */
test('视觉修订、重复字幕和强度变化均不重演已开始手势', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  let peak = 0;
  for (let frame = 0; frame <= 240; frame++) {
    const time = frame / 60;
    const original = cue('first', 'wave');
    const update = { ...original, id: 'revision-' + frame, energy: frame > 20 ? 0 : 1 };
    const lip = { open: 0.5, form: 0.2 };
    const changed = actual.advance(input({ time, delivery: update, lip }));
    const reference = control.advance(input({ time, delivery: original, lip }));
    assert.deepEqual(changed, reference);
    peak = Math.max(peak, changed.ParamArmL);
    if (frame > 170) assert.ok(changed.ParamArmL < 1e-6);
  }
  assert.ok(peak > 0.65);
});

/** 真正起音到来后才发力；准备幅度很小且逾期会自然收回，不从字幕推测重音。 */
test('准备等待实际音频，静音不发力，有起音则完成一次动作', () => {
  const active = new AvatarPresentation();
  const silent = new AvatarPresentation();
  const delivery = { ...cue('wave', 'wave'), gestureSource: 'dialogue' };
  let earlyPeak = 0;
  let voicedPeak = 0;
  let silentPeak = 0;
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60;
    const audio = { at: time, energy: frame >= 30 ? 0.7 : 0, onset: frame === 30 ? 0.7 : 0 };
    const voiced = active.advance(input({ time, delivery: { ...delivery, audio } }));
    const quiet = silent.advance(
      input({ time, delivery: { ...delivery, audio: { ...audio, energy: 0, onset: 0 } } })
    );
    if (frame < 30) earlyPeak = Math.max(earlyPeak, voiced.ParamArmL);
    voicedPeak = Math.max(voicedPeak, voiced.ParamArmL);
    silentPeak = Math.max(silentPeak, quiet.ParamArmL);
    assert.equal(voiced.ParamMouthOpenY, 0);
    assert.equal(voiced.ParamMouthForm, 0);
    if (frame === 180) assert.ok(quiet.ParamArmL < 1e-6);
  }
  assert.ok(earlyPeak > 0 && earlyPeak < 0.12);
  assert.ok(silentPeak < 0.12);
  assert.ok(voicedPeak > 0.29 && voicedPeak < 0.45);
});

/** 倾听和指令不依赖PCM，自然说话只采用轻幅度；回执来自实际动作时钟。 */
test('指令无声完成且只有真实发力后报告started/completed，倾听幅度约30%', () => {
  const command = new AvatarPresentation();
  const listening = new AvatarPresentation();
  const speaking = new AvatarPresentation();
  const peaks = [0, 0, 0];
  const acknowledgments = [];
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60;
    for (const [index, runtime] of [command, listening, speaking].entries()) {
      const delivery = {
        ...cue('wave', 'wave'),
        phase: index === 0 ? 'idle' : index === 1 ? 'listening' : 'speaking',
        gestureSource: index === 0 ? 'command' : 'dialogue',
      };
      const observed = runtime.advance(
        input({ time, delivery, lip: index === 2 ? { open: 0.5, form: 0 } : null })
      );
      peaks[index] = Math.max(peaks[index], observed.ParamArmL);
      if (index === 0) {
        assert.equal(observed.ParamMouthOpenY, 0);
        acknowledgments.push(...runtime.drainAcknowledgments());
      }
    }
  }
  assert.deepEqual(
    acknowledgments.map((ack) => ack.status),
    ['started', 'completed']
  );
  assert.ok(peaks[0] > 0.65);
  assert.ok(Math.abs(peaks[1] / peaks[0] - 0.3) < 0.02);
  assert.ok(Math.abs(peaks[2] / peaks[0] - 0.45) < 0.02);
});

/** 自然也回报原stroke，指令断言按身份筛选；冷却限制和原运动期望保持不变。 */
test('自然动作满足3秒、同类6秒及10秒两次上限，明确指令绕过冷却', () => {
  const runtime = new AvatarPresentation();
  const started = [];
  const schedule = [
    [0, 'wave'],
    [2, 'nod'],
    [3.2, 'nod'],
    [6.4, 'tilt'],
    [10.2, 'wave'],
    [11, 'wave'],
  ];
  for (let frame = 0; frame <= 14 * 60; frame++) {
    const time = frame / 60;
    const index = schedule.findLastIndex(([at]) => time >= at);
    const [at, gesture] = schedule[index];
    const source = at === 11 ? 'command' : 'dialogue';
    const delivery = {
      ...cue(`event-${at}`, gesture),
      gestureSource: source,
      phase: source === 'command' ? 'idle' : 'listening',
    };
    runtime.advance(input({ time, delivery }));
    if (runtime.activeGesture?.stroke === time) started.push([at, gesture]);
  }
  assert.deepEqual(started, [
    [0, 'wave'],
    [3.2, 'nod'],
    [10.2, 'wave'],
    [11, 'wave'],
  ]);
  assert.deepEqual(
    runtime
      .drainAcknowledgments()
      .filter((ack) => ack.gesture_id === 'event-11')
      .map((ack) => ack.status),
    ['started', 'completed']
  );
});

/** 取消必须保留未完成事实，后续同身份更新不能补演；新指令可平滑抢占。 */
test('新指令抢占旧指令报告取消，语音phase变化不重启当前指令', () => {
  const runtime = new AvatarPresentation();
  const events = [];
  let last;
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60;
    const delivery =
      frame < 25
        ? cue('old', 'wave')
        : { ...cue('new', 'shake'), id: `phase-${frame}`, phase: frame > 90 ? 'idle' : 'thinking' };
    last = runtime.advance(input({ time, delivery }));
    events.push(...runtime.drainAcknowledgments());
  }
  assert.deepEqual(
    events.map((ack) => [ack.gesture_id, ack.status]),
    [
      ['old', 'started'],
      ['old', 'canceled'],
      ['new', 'started'],
      ['new', 'completed'],
    ]
  );
  assert.ok(Math.abs(last.ParamArmL) < 1e-6);
});

/** 在真实帧序列检查两侧位移和双臂内收；所有五种动作必须有效且保持嘴部独占。 */
test('五种手势准备发力收回，保留口型独占与主要运动幅度', () => {
  for (const gesture of ['nod', 'tilt', 'wave', 'shy', 'shake']) {
    const runtime = new AvatarPresentation();
    const delivery = cue(gesture, gesture);
    const peaks = { arm: 0, inward: 0, down: 0, left: 0, right: 0, tilt: 0 };
    let last;
    for (let frame = 0; frame <= 240; frame++) {
      const time = frame / 60;
      const lip = frame < 150 ? { open: 0.63, form: -0.4 } : null;
      last = runtime.advance(input({ time, delivery, lip }));
      peaks.arm = Math.max(peaks.arm, last.ParamArmL);
      peaks.inward = Math.min(peaks.inward, last.ParamArmL);
      peaks.down = Math.min(peaks.down, last.ParamAngleY);
      peaks.left = Math.min(peaks.left, last.ParamAngleX);
      peaks.right = Math.max(peaks.right, last.ParamAngleX);
      peaks.tilt = Math.max(peaks.tilt, last.ParamAngleZ);
      assert.equal(last.ParamMouthOpenY, lip?.open ?? 0);
      assert.equal(last.ParamMouthForm, lip?.form ?? 0);
      if (gesture === 'shy') assert.equal(last.ParamArmL, last.ParamArmR);
    }
    if (gesture === 'wave') assert.ok(peaks.arm > 0.65);
    if (gesture === 'shy') assert.ok(peaks.inward < -0.5 && peaks.down < -12);
    if (gesture === 'shake') assert.ok(peaks.left < -8 && peaks.right > 8);
    if (gesture === 'nod') assert.ok(peaks.down < -8);
    if (gesture === 'tilt') assert.ok(peaks.tilt > 12);
    assert.ok(Math.abs(last.ParamArmL) < 1e-6);
    assert.ok(Math.abs(last.ParamAngleY) < 1e-6);
    assert.ok(Math.abs(last.ParamAngleX) < 1e-6);
  }
});

/** 真实失败发生于负向待机歪头，单独保留固定门回归，防止其它动作先失败而掩盖该复现。 */
test('energy .45歪头不被负向待机抵消，原Z峰值与跨度门保持不变', (context) => {
  const rows = naturalMotionTrace('tilt', { startTime: 7 });
  const z = rows.map((row) => row.output.ParamAngleZ);
  const peak = Math.max(...z),
    span = peak - Math.min(...z);
  assert.ok(peak > 4.5 && span > 2.5, JSON.stringify({ energy: 0.45, peak, span }));
  context.diagnostic(JSON.stringify({ energy: 0.45, peak, span, peakGate: 4.5, spanGate: 2.5 }));
});

/** 固定沿用真实房间的五动作门，跨正反待机相位和视线只验证主动作接管，不抬高强度或放宽门。 */
test('energy .45自然动作在各待机相位满足原运动门，表情和口型仍独立', () => {
  for (const fps of [30, 60])
    for (const startTime of [2, 4.5, 7, 9.5, 12])
      for (const gazeX of [-1, 1]) {
        for (const gesture of ['nod', 'tilt', 'wave', 'shy', 'shake']) {
          const rows = naturalMotionTrace(gesture, { fps, startTime, gazeX });
          const values = rows.map((row) => row.output);
          const x = values.map((value) => value.ParamAngleX),
            y = values.map((value) => value.ParamAngleY);
          const z = values.map((value) => value.ParamAngleZ),
            armL = values.map((value) => value.ParamArmL);
          const armR = values.map((value) => value.ParamArmR);
          const label = `${gesture} fps=${fps} start=${startTime} gaze=${gazeX}`;
          if (gesture === 'tilt')
            assert.ok(Math.max(...z) > 4.5 && Math.max(...z) - Math.min(...z) > 2.5, label);
          if (gesture === 'nod')
            assert.ok(Math.min(...y) < -2 && Math.max(...y) - Math.min(...y) > 4, label);
          if (gesture === 'wave') assert.ok(Math.max(...armL) > 0.16, label);
          if (gesture === 'shy')
            assert.ok(
              Math.min(...armL) < -0.1 && Math.min(...armR) < -0.1 && Math.min(...y) < -2,
              label
            );
          if (gesture === 'shake')
            assert.ok(
              Math.min(...x) < -2 && Math.max(...x) > 4 && Math.max(...x) - Math.min(...x) > 5,
              label
            );
          for (const row of rows) {
            assert.equal(row.output.ParamMouthOpenY, row.lip?.open ?? 0);
            assert.equal(row.output.ParamMouthForm, row.lip?.form ?? 0);
            assert.equal(row.output.ParamBrowLY, row.baseline.ParamBrowLY);
            assert.equal(row.output.ParamBrowRY, row.baseline.ParamBrowRY);
          }
        }
      }
});

/** 校准替换旧参数振幅fixture，使用实际MOC物理角度而不降低可见门；原步长、取消和收敛门保持。 */
test('自然主姿态保持能量层次，取消与结束平滑归还待机且当帧闭嘴', async (context) => {
  const peaks = [0, 0.45, 1].map((energy) =>
    Math.max(...naturalMotionTrace('tilt', { energy }).map((row) => row.output.ParamAngleZ))
  );
  assert.ok(peaks[0] < peaks[1] && peaks[1] < peaks[2]);
  assert.ok(peaks.every((peak) => peak > 4.5));
  const { directory, manifest } = resourceExpressions();
  const { moc, model } = await productionCoreModel(directory, manifest);
  let angles;
  try {
    modelVertices(model, {});
    const index = model.drawables.ids.indexOf('Head');
    const neutral = Array.from(model.drawables.vertexPositions[index]);
    /** 对同索引头网格刚性拟合，模型参数30不再被误当成肉眼旋转30度。 */
    const angle = (z) => {
      modelVertices(model, { ParamAngleZ: z });
      const actual = Array.from(model.drawables.vertexPositions[index]);
      /** 先移除各自平移，避免身摆或画布原点被误算成头部旋转。 */
      const center = (values) =>
        [0, 1].map(
          (axis) =>
            values.filter((_, i) => i % 2 === axis).reduce((a, b) => a + b, 0) / (values.length / 2)
        );
      const a = center(neutral),
        b = center(actual);
      let dot = 0,
        cross = 0;
      for (let i = 0; i < neutral.length; i += 2) {
        const x = neutral[i] - a[0],
          y = neutral[i + 1] - a[1];
        const u = actual[i] - b[0],
          v = actual[i + 1] - b[1];
        dot += x * u + y * v;
        cross += x * v - y * u;
      }
      return Math.abs((Math.atan2(cross, dot) * 180) / Math.PI);
    };
    angles = peaks.map(angle);
    assert.ok(angles[0] < angles[1] && angles[1] < angles[2]);
    assert.ok(angles.every((value) => value > 0 && value <= 4.8));
    assert.ok(angles[1] >= 2.3);
    assert.ok(angle(30) <= 7.6);
  } finally {
    model.release();
    moc._release();
  }
  const maximumSteps = { 30: 0, 60: 0 };
  for (const fps of [30, 60])
    for (const gesture of ['nod', 'tilt', 'wave', 'shy', 'shake']) {
      for (const cancelAt of [0.6, Infinity]) {
        const rows = naturalMotionTrace(gesture, { fps, gazeX: -1, cancelAt });
        for (const row of rows) {
          for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ']) {
            const step = Math.abs(row.output[id] - row.previous[id]);
            maximumSteps[fps] = Math.max(maximumSteps[fps], step);
            assert.ok(
              step < (fps === 60 ? 2 : 3),
              `${gesture} ${id} fps=${fps} t=${row.elapsed} step=${row.output[id] - row.previous[id]} sudden step`
            );
          }
          if (row.elapsed >= cancelAt) {
            assert.equal(row.output.ParamMouthOpenY, 0);
            assert.equal(row.output.ParamMouthForm, 0);
          }
        }
        const last = rows.at(-1);
        for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamArmL', 'ParamArmR'])
          assert.ok(
            Math.abs(last.output[id] - last.baseline[id]) < 1e-6,
            `${gesture} ${id} did not recover`
          );
      }
    }
  context.diagnostic(
    JSON.stringify({
      energies: [0, 0.45, 1],
      tiltPeaks: peaks,
      physicalAngles: angles,
      maximumSteps,
    })
  );
});

/** 主轴只让出背景微动，表情 Add/Overwrite 的语义偏移必须保留，不能用整轴清零掩盖抵消。 */
test('主姿态接管仍保留Jev角度表情偏移', () => {
  for (const blend of ['Add', 'Overwrite']) {
    const expressive = new AvatarPresentation({
      curious: expression([{ Id: 'ParamAngleZ', Value: 2.5, Blend: blend }]),
    });
    const neutral = new AvatarPresentation();
    let difference;
    for (let frame = -120; frame <= 42; frame++) {
      const active = frame >= 0;
      const delivery = {
        ...cue('expression-tilt', 'tilt', 'turn', 'curious'),
        gestureSource: 'dialogue',
        energy: 0.45,
      };
      const state = input({
        time: 7 + frame / 60,
        delivery: active ? delivery : null,
        behavior: active ? 'speaking' : 'idle',
        lip: active ? { open: 0.5, form: -0.2 } : null,
      });
      const actual = expressive.advance(state);
      const reference = neutral.advance(state);
      difference = actual.ParamAngleZ - reference.ParamAngleZ;
      assert.equal(actual.ParamMouthOpenY, reference.ParamMouthOpenY);
      assert.equal(actual.ParamMouthForm, reference.ParamMouthForm);
    }
    assert.ok(Math.abs(difference - 2.5) < 0.01, `${blend} lost expression offset: ${difference}`);
  }
});

/** 取消保留姿态和速度使手臂自然收回，嘴必须同一帧归零；迟到更新与旧动作均不能复活。 */
test('打断立即闭嘴并自然收束，只有新回复可以再次动作', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  let firstPeak = 0;
  let newPeak = 0;
  for (let frame = 0; frame <= 300; frame++) {
    const time = frame / 60;
    let expected = frame < 48 ? cue('first', 'wave') : null;
    if (frame >= 180) expected = cue('new', 'wave', 'reply-b');
    let received = expected;
    if (frame >= 90 && frame < 180) received = cue('late-' + frame, 'wave');
    const lip = frame < 48 || frame >= 180 ? { open: 0.5, form: 0.2 } : null;
    const changed = actual.advance(input({ time, delivery: received, lip }));
    const baseline = control.advance(input({ time, delivery: expected, lip }));
    assert.deepEqual(changed, baseline);
    if (frame < 48) firstPeak = Math.max(firstPeak, changed.ParamArmL);
    if (frame === 48) {
      assert.ok(changed.ParamArmL > 0);
      assert.equal(changed.ParamMouthOpenY, 0);
      assert.equal(changed.ParamMouthForm, 0);
    }
    if (frame === 120) assert.ok(changed.ParamArmL < 1e-6);
    if (frame >= 180) newPeak = Math.max(newPeak, changed.ParamArmL);
  }
  assert.ok(firstPeak > 0.65 && newPeak > 0.65);
});

/** 常驻舞台跨 160 个回复复用动作名，检验实际出力与取消姿态，防止只测集合清空而漏掉旧动作复活。 */
test('160轮回复的动作身份有作用域，取消后的迟到已执行动作不能补演', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  for (let reply = 0; reply < 160; reply++) {
    const wave = { ...cue(`wave-${reply}`, 'wave', `reply-${reply}`), gestureId: 'wave' };
    const nod = { ...cue(`nod-${reply}`, 'nod', `reply-${reply}`), gestureId: 'nod' };
    let peak = 0;
    for (let frame = 0; frame <= 240; frame++) {
      const time = reply * 5 + frame / 60;
      const expected = frame < 110 ? wave : frame < 120 ? nod : null;
      const received = frame >= 160 ? { ...wave, id: `late-${reply}-${frame}` } : expected;
      const lip = frame < 120 ? { open: 0.5, form: 0 } : null;
      const observed = actual.advance(input({ time, delivery: received, lip }));
      const reference = control.advance(input({ time, delivery: expected, lip }));
      assert.deepEqual(observed, reference, `reply ${reply}, frame ${frame}`);
      if (frame < 110) peak = Math.max(peak, observed.ParamArmL);
      if (frame >= 120) assert.equal(observed.ParamMouthOpenY, 0);
    }
    assert.ok(peak > 0.65, `reply ${reply} must execute its own new wave`);
    assert.ok(actual.seenGestures.size <= 128, '常驻舞台仅保留有界执行身份');
  }
});

/** 按相同实际时间采样，允许离散起音检测一帧误差；后台大跳跃作废等待动作且数值有界。 */
test('30和60fps动作节奏一致，长暂停不补播且损坏输入有界', () => {
  const samples = [];
  for (const fps of [30, 60]) {
    const runtime = new AvatarPresentation();
    const trajectory = [];
    for (let frame = 0; frame <= fps * 3; frame++) {
      const time = frame / fps;
      const output = runtime.advance(
        input({ time, delta: 1 / fps, delivery: cue('wave', 'wave'), lip: { open: 0.5, form: 0 } })
      );
      if (frame % (fps / 10) === 0) trajectory.push(output.ParamArmL);
    }
    samples.push(trajectory);
  }
  for (let index = 0; index < samples[0].length; index++) {
    assert.ok(Math.abs(samples[0][index] - samples[1][index]) < 0.055, 'same-time arm amplitude');
  }
  const runtime = new AvatarPresentation();
  runtime.advance(input({ delivery: cue('paused', 'wave') }));
  const paused = runtime.advance(
    input({ time: 100, delta: 60, delivery: cue('paused', 'wave'), lip: { open: 0.6, form: 0 } })
  );
  assert.ok(Math.abs(paused.ParamArmL) < 0.12);
  assert.ok(
    settle(runtime, { time: 100.5, delivery: cue('paused', 'wave'), lip: { open: 0.6, form: 0 } })
      .ParamArmL < 1e-6
  );
  const malformed = runtime.advance(
    input({
      time: NaN,
      delta: Infinity,
      gaze: { x: NaN, y: Infinity },
      lip: { open: NaN, form: Infinity },
    })
  );
  assert.ok(Object.values(malformed).every(Number.isFinite));
  assert.equal(malformed.ParamMouthOpenY, 0);
});

/** 语义表情至少保持短时间；同一动作期间的神态更新不会触碰肢体或口型通道。 */
test('短时间不同表情保持稳定，到保持期限后才淡化更新', () => {
  const runtime = new AvatarPresentation({
    happy: expression([{ Id: 'ParamBrowLY', Value: 0.3, Blend: 'Add' }]),
    concerned: expression([{ Id: 'ParamBrowLY', Value: -0.3, Blend: 'Add' }]),
  });
  let output;
  for (let frame = 0; frame <= 120; frame++) {
    const time = frame / 60;
    const style = frame < 30 ? 'happy' : 'concerned';
    output = runtime.advance(
      input({ time, delivery: cue('frame-' + frame, 'none', 'reply-a', style) })
    );
    if (frame === 60) assert.ok(output.ParamBrowLY > 0.25);
  }
  assert.ok(output.ParamBrowLY < -0.25);
});

/** 缺失当前协议身份不允许启动动作，默认 neutral 也不能成为旧调用的隐式兼容层。 */
test('缺少phase、gestureId或replyKey的调用不启动手势', () => {
  for (const missing of ['phase', 'gestureId', 'replyKey']) {
    const runtime = new AvatarPresentation();
    const delivery = cue('invalid', 'wave');
    delete delivery[missing];
    let peak = 0;
    for (let frame = 0; frame <= 120; frame++) {
      const output = runtime.advance(
        input({ time: frame / 60, delivery, lip: { open: 0.6, form: 0 } })
      );
      peak = Math.max(peak, output.ParamArmL);
    }
    assert.equal(peak, 0);
  }
});

/** 名称和参数均用白名单，资源损坏不能静默引入未知目标或跨通道竞争。 */
test('未知表情、重复参数和越界值被明确拒绝', () => {
  assert.equal(isAvatarExpressionName('happy'), true);
  assert.equal(isAvatarExpressionName('../../private'), false);
  assert.throws(() => parseAvatarExpression(null), /无效/);
  assert.throws(() => expression([{ Id: 'ParamUnknown', Value: 0, Blend: 'Add' }]), /无效/);
  assert.throws(() => expression([{ Id: 'ParamBrowLY', Value: 9, Blend: 'Add' }]), /越界/);
  assert.throws(() => expression([{ Id: 'ParamBrowLY', Value: NaN, Blend: 'Add' }]), /无效/);
  assert.throws(
    () =>
      expression([
        { Id: 'ParamBrowLY', Value: 0.1, Blend: 'Add' },
        { Id: 'ParamBrowLY', Value: 0.2, Blend: 'Add' },
      ]),
    /无效/
  );
});

/** 正式 manifest 声明的现有表情必须全部可读，防止测试只覆盖代码中手写的理想资源。 */
test('正式模型的表情资源均被运行时支持', () => {
  const directory = new URL('../public/avatar/xiaoya/', import.meta.url);
  const manifest = JSON.parse(readFileSync(new URL('xiaoya.model3.json', directory), 'utf8'));
  for (const reference of manifest.FileReferences.Expressions) {
    assert.equal(isAvatarExpressionName(reference.Name), true);
    assert.doesNotThrow(() =>
      parseAvatarExpression(JSON.parse(readFileSync(new URL(reference.File, directory), 'utf8')))
    );
  }
});

/** 七种语义使用真实眉眼和视线区分；害羞避开视线，惊讶对称提眉但不虚构张嘴或超范围睁眼。 */
test('正式七风格拥有不同面部特征，害羞避开视线且惊讶提眉聚焦', () => {
  const { expressions } = resourceExpressions();
  assert.equal(expressions.neutral, undefined);
  const runtime = new AvatarPresentation(expressions);
  const signatures = new Set();
  const outputs = {};
  for (const style of ['happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised', 'neutral']) {
    const output = settle(runtime, {
      time: Object.keys(outputs).length * 2,
      delivery: cue(style, 'none', 'reply-a', style),
    });
    outputs[style] = output;
    const signature = [];
    for (const id of [
      'ParamEyeSmile',
      'ParamBrowLY',
      'ParamBrowRY',
      'ParamBrowLAngle',
      'ParamBrowRAngle',
    ]) {
      signature.push(output[id].toFixed(3));
    }
    signatures.add(signature.join(','));
  }
  assert.equal(signatures.size, 7);
  assert.ok(Math.abs(outputs.happy.ParamEyeSmile - 0.52) < 1e-6);
  assert.ok(Math.abs(outputs.gentle.ParamEyeSmile - 0.16) < 1e-6);
  assert.ok(outputs.concerned.ParamEyeSmile < 1e-6);
  assert.ok(outputs.concerned.ParamBrowLAngle < -0.65);
  assert.ok(outputs.concerned.ParamBrowRAngle > 0.65);
  assert.ok(outputs.curious.ParamBrowLAngle > 0.3);
  assert.ok(outputs.curious.ParamBrowRAngle < -0.1);
  assert.ok(outputs.shy.ParamEyeBallX < -0.39);
  assert.ok(outputs.shy.ParamEyeBallY < -0.54);
  assert.ok(outputs.shy.ParamEyeLOpen < outputs.gentle.ParamEyeLOpen - 0.15);
  assert.ok(outputs.shy.ParamEyeSmile < outputs.gentle.ParamEyeSmile);
  assert.ok(outputs.shy.ParamBrowLAngle < -0.34);
  assert.ok(outputs.shy.ParamBrowRAngle > 0.34);
  assert.ok(outputs.surprised.ParamBrowLY > 0.99);
  assert.ok(outputs.surprised.ParamBrowRY > 0.99);
  assert.ok(Math.abs(outputs.surprised.ParamEyeLOpen - 1) < 1e-6);
  assert.ok(Math.abs(outputs.surprised.ParamEyeROpen - 1) < 1e-6);
  assert.ok(Math.abs(outputs.surprised.ParamEyeBallX) < 1e-6);
  assert.ok(Math.abs(outputs.surprised.ParamEyeBallY) < 1e-6);
  assert.ok(outputs.surprised.ParamEyeSmile < 1e-6);
  for (const id of [
    'ParamEyeSmile',
    'ParamBrowLY',
    'ParamBrowRY',
    'ParamBrowLAngle',
    'ParamBrowRAngle',
  ]) {
    assert.ok(Math.abs(outputs.neutral[id]) < 1e-6, `neutral ${id}`);
  }
  assert.ok(Math.abs(outputs.neutral.ParamEyeLOpen - 1) < 1e-6);
  assert.ok(Math.abs(outputs.neutral.ParamEyeROpen - 1) < 1e-6);
  for (const behavior of ['listening', 'thinking']) {
    const output = settle(runtime, { behavior });
    assert.ok(output.ParamEyeSmile < 1e-6);
    assert.ok(Math.abs(output.ParamBrowLAngle) < 1e-6);
    assert.ok(Math.abs(output.ParamBrowRAngle) < 1e-6);
  }
});

/** 七风格资源均只拥有眉眼；惊讶不能假造张嘴，害羞也不能覆盖眨眼，最终嘴仍由真实音频控制。 */
test('全部正式表情保留口型独占与闭眼优先级', () => {
  const { expressions } = resourceExpressions();
  for (const expression of Object.values(expressions)) {
    for (const parameter of expression.parameters) assert.match(parameter.id, /^Param(Eye|Brow)/);
  }
  for (const style of ['neutral', 'happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised']) {
    const runtime = new AvatarPresentation(expressions);
    const delivery = cue(style, 'none', 'reply-a', style);
    const output = settle(runtime, { delivery, blink: 0, lip: { open: 0.63, form: -0.4 } });
    assert.equal(output.ParamEyeLOpen, 0);
    assert.equal(output.ParamEyeROpen, 0);
    assert.equal(output.ParamMouthOpenY, 0.63);
    assert.equal(output.ParamMouthForm, -0.4);
    const silent = runtime.advance(input({ delivery }));
    assert.equal(silent.ParamMouthOpenY, 0);
    assert.equal(silent.ParamMouthForm, 0);
  }
});

/** 所有真实分层部件共同验收表情与嘴眼安全；坐标规范化保留同一像素差异门，拒绝参数变化但几何不动。 */
test('正式 Core 的笑眼眉角真实绑定且合成表情不翻折', async (context) => {
  const { directory, manifest, expressions } = resourceExpressions();
  const { moc, model } = await productionCoreModel(directory, manifest);
  try {
    assert.equal(model.parameters.count, 19);
    const { indices, drawables } = modelTopology(model);
    for (const id of ['Head', 'BrowL', 'BrowR', 'EyeWhiteL', 'EyeWhiteR', 'ArmL2', 'ArmR2']) {
      assert.ok(drawables.has(id), `必须保留独立表情或肢体网格 ${id}`);
    }
    const reference = modelVertices(model, { ParamMouthOpenY: 1 });
    for (const id of ['ParamEyeSmile', 'ParamBrowLAngle', 'ParamBrowRAngle']) {
      const changed = modelVertices(model, { ParamMouthOpenY: 1, [id]: 0.7 });
      assert.ok(maximumDelta(reference, changed) > 0.003, `${id} must deform real geometry`);
    }
    const signs = [];
    for (let index = 0; index < indices.length; index += 3) {
      const area = triangleArea(reference, indices, index);
      assert.ok(Number.isFinite(area) && area !== 0, '真实参考拓扑不能退化');
      signs.push(Math.sign(area));
    }
    const geometry = {};
    for (const style of [
      'neutral',
      'happy',
      'gentle',
      'concerned',
      'curious',
      'shy',
      'surprised',
    ]) {
      const delivery = cue(style, 'none', 'reply-a', style);
      const output = settle(new AvatarPresentation(expressions), { delivery });
      geometry[style] = modelVertices(model, output);
      for (const blink of [0, 0.5, 1]) {
        for (const open of [0, 0.55, 1]) {
          const pose = settle(new AvatarPresentation(expressions), {
            delivery,
            blink,
            lip: { open, form: 0.2 },
          });
          assert.equal(pose.ParamMouthOpenY, open);
          assert.equal(pose.ParamMouthForm, 0.2);
          const vertices = modelVertices(model, pose);
          for (let index = 0; index < indices.length; index += 3) {
            const signed = triangleArea(vertices, indices, index) * signs[index / 3];
            assert.ok(Number.isFinite(signed) && signed > 0, `${style}/${blink}/${open}/${index}`);
          }
        }
      }
    }
    assert.ok(maximumDelta(geometry.happy, geometry.gentle) > 0.01);
    assert.ok(maximumDelta(geometry.concerned, geometry.curious) > 0.01);
    assert.ok(
      maximumDelta(geometry.shy, geometry.gentle) > 0.01,
      `shy/gentle ${maximumDelta(geometry.shy, geometry.gentle) * 229}px@458`
    );
    assert.ok(
      maximumDelta(geometry.shy, geometry.happy) > 0.01,
      `shy/happy ${maximumDelta(geometry.shy, geometry.happy) * 229}px@458`
    );
    context.diagnostic(
      `458px原画布几何差：害羞/温柔 ${(maximumDelta(geometry.shy, geometry.gentle) * 229).toFixed(3)}px；害羞/开心 ${(maximumDelta(geometry.shy, geometry.happy) * 229).toFixed(3)}px，非视觉验收`
    );
    assert.ok(maximumDelta(geometry.surprised, geometry.neutral) > 0.01);
    assert.ok(maximumDelta(geometry.surprised, geometry.curious) > 0.01);
    context.diagnostic(
      `458px原画布几何差：惊讶/中性 ${(maximumDelta(geometry.surprised, geometry.neutral) * 229).toFixed(3)}px；惊讶/好奇 ${(maximumDelta(geometry.surprised, geometry.curious) * 229).toFixed(3)}px，睁眼不超过正式模型上限`
    );
    const inward = modelVertices(model, { ParamMouthOpenY: 1, ParamArmL: -0.55, ParamArmR: -0.55 });
    const leftHand = drawables.get('ArmL2');
    const rightHand = drawables.get('ArmR2');
    assert.ok(
      drawableCenterX(inward, leftHand) > drawableCenterX(reference, leftHand),
      '左臂负参数必须向身体中心移动'
    );
    assert.ok(
      drawableCenterX(inward, rightHand) < drawableCenterX(reference, rightHand),
      '右臂负参数必须向身体中心移动'
    );
  } finally {
    model.release();
    moc._release();
  }
});
