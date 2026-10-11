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

/** 只检查实际渲染使用的参数过渡，不维护一个没有生产消费者的 easing 测试副本。 */
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
  assert.ok(at30.ParamAngleX > 5.99 && at30.ParamAngleX < 6);
  const paused = new AvatarPresentation();
  const first = paused.advance(input({ behavior: 'confused', delta: 600 }));
  assert.ok(first.ParamAngleZ > -7 && first.ParamAngleZ < 0);
  assert.equal(
    paused.advance(input({ behavior: 'confused', delta: 0 })).ParamAngleZ,
    first.ParamAngleZ
  );
});

/** 全身父级需要真正收到轻微联动，避免模型有身摆绑定而页面仍像固定底座；不复制耦合公式。 */
test('头部转向带动同向小幅身摆，视线与音频口型仍各自拥有参数', () => {
  const right = settle(new AvatarPresentation(), { gaze: { x: 1, y: 0.4 } });
  const left = settle(new AvatarPresentation(), { gaze: { x: -1, y: -0.4 } });
  assert.ok(right.ParamBodyAngleX > 0.5 && right.ParamBodyAngleX < right.ParamAngleX);
  assert.ok(left.ParamBodyAngleX < -0.5 && left.ParamBodyAngleX > left.ParamAngleX);
  assert.ok(Math.abs(right.ParamEyeBallX - 1) < 1e-12);
  assert.ok(Math.abs(left.ParamEyeBallX + 1) < 1e-12);
  assert.ok(Math.abs(right.ParamEyeBallY - 0.4) < 1e-12);
  assert.ok(Math.abs(left.ParamEyeBallY + 0.4) < 1e-12);
  assert.equal(right.ParamMouthOpenY, 0);
  assert.equal(left.ParamMouthOpenY, 0);
});

/** 用真实动作及取消比较节奏与收回，避免另一个全身循环在打断后继续表演。 */
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
      const at30 = slow.advance({ ...state, delta: 1 / 30 });
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
    replyKey: JSON.stringify(['job-a', reply]),
    gesture,
    style,
    phase: 'speaking',
    turnId: 'turn-a',
    segmentKey: id,
    gestureId: id,
    energy: 1,
  };
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

/** 每个姿态重置 Core 参数，排除上一姿态残留或 Add 累加造成的假位移。 */
function modelVertices(model, values) {
  model.parameters.values.set(model.parameters.defaultValues);
  for (const [name, value] of Object.entries(values)) {
    const index = model.parameters.ids.indexOf(name);
    if (index >= 0) model.parameters.values[index] = value;
  }
  model.update();
  return Float32Array.from(model.drawables.vertexPositions[0]);
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
  const recovered = settle(runtime);
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
  const delivery = cue('wave', 'wave');
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
  assert.ok(voicedPeak > 0.65);
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
    assert.equal(actual.seenGestures.size, 2, 'only this reply owns its consumed wave and nod');
  }
});

/** 相同动作连续分句合并；完成后的静止间隔允许下一次动作，取消不能冒充完成。 */
test('连续同款合并且none间隔只在自然完成后开放', () => {
  for (const cancelled of [false, true]) {
    const runtime = new AvatarPresentation();
    let latePeak = 0;
    for (let frame = 0; frame <= 300; frame++) {
      let delivery = cue('first', 'wave');
      if (frame >= 20) delivery = cue('quiet', 'none');
      if (frame >= 45 && frame < 170) delivery = cancelled ? null : cue('quiet', 'none');
      if (frame >= 170 && frame < 190) delivery = cue('late-none', 'none');
      if (frame >= 190) delivery = cue('next', 'wave');
      const output = runtime.advance(
        input({ time: frame / 60, delivery, lip: { open: 0.5, form: 0 } })
      );
      if (frame >= 190) latePeak = Math.max(latePeak, output.ParamArmL);
    }
    if (cancelled) assert.ok(latePeak < 1e-6);
    else assert.ok(latePeak > 0.65);
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

/** 七种表情须真实形变且嘴眼组合安全；害羞区别温柔/开心，惊讶区别中性/好奇，不降低几何差异门。 */
test('正式 Core 的笑眼眉角真实绑定且合成表情不翻折', async (context) => {
  const { directory, manifest, expressions } = resourceExpressions();
  const { moc, model } = await productionCoreModel(directory, manifest);
  try {
    assert.equal(model.parameters.count, 19);
    assert.equal(model.drawables.count, 1);
    const reference = modelVertices(model, { ParamMouthOpenY: 1 });
    for (const id of ['ParamEyeSmile', 'ParamBrowLAngle', 'ParamBrowRAngle']) {
      const changed = modelVertices(model, { ParamMouthOpenY: 1, [id]: 0.7 });
      assert.ok(maximumDelta(reference, changed) > 0.003, `${id} must deform real geometry`);
    }
    const indices = model.drawables.indices[0];
    const signs = [];
    for (let index = 0; index < indices.length; index += 3) {
      signs.push(Math.sign(triangleArea(reference, indices, index)));
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
    const leftHand = (59 * 81 + 24) * 2;
    const rightHand = (59 * 81 + 54) * 2;
    assert.ok(inward[leftHand] > reference[leftHand], '左臂负参数必须向身体中心移动');
    assert.ok(inward[rightHand] < reference[rightHand], '右臂负参数必须向身体中心移动');
  } finally {
    model.release();
    moc._release();
  }
});
