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
  return { id, replyKey: JSON.stringify(['job-a', reply]), gesture, style };
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

/** 一句话只能启动一次短动作；迟到重复 cue、静音恢复或过期帧都不能续演旧动作。 */
test('短手势只启动一次，结束、打断、重发后均回中性', () => {
  const runtime = new AvatarPresentation();
  const delivery = cue('speech-1', 'wave');
  runtime.advance(input({ delivery }));
  let peak = 0;
  for (let frame = 1; frame <= 180; frame++) {
    const output = runtime.advance(input({ time: frame / 60, delivery }));
    peak = Math.max(peak, output.ParamArmL);
  }
  assert.ok(peak > 0.65, `wave peak ${peak}`);
  assert.ok(settle(runtime, { time: 4, delivery }).ParamArmL < 1e-6);
  settle(runtime, { time: 5 });
  assert.ok(settle(runtime, { time: 5.5, delivery }).ParamArmL < 1e-6);

  const replacement = cue('speech-2', 'wave', 'reply-b');
  runtime.advance(input({ time: 6, delivery: replacement }));
  assert.ok(settle(runtime, { time: 6.8, delivery: replacement }, 20).ParamArmL > 0.5);
  const firstStop = runtime.advance(input({ time: 6.9 }));
  assert.ok(firstStop.ParamArmL > 0, '取消平滑收尾而非生硬跳回');
  assert.ok(settle(runtime, { time: 7 }).ParamArmL < 1e-6);
});

/** 主动作互斥；新句的点头不能继续上一句挥手，后台长暂停也不应补播动作队列。 */
test('新动作替换旧动作，长帧间隔不补播且数值有限', () => {
  const runtime = new AvatarPresentation();
  const wave = cue('wave', 'wave');
  runtime.advance(input({ delivery: wave }));
  settle(runtime, { time: 0.8, delivery: wave });
  const nod = cue('nod');
  runtime.advance(input({ time: 1, delivery: nod }));
  const output = settle(runtime, { time: 1.3, delivery: nod });
  assert.ok(output.ParamArmL < 1e-6);
  assert.ok(output.ParamAngleY < -5);
  const resumed = settle(runtime, { time: 100, delta: 60, delivery: nod });
  assert.ok(Math.abs(resumed.ParamAngleY) < 1e-6);
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
  assert.equal(malformed.ParamMouthForm, 0);
});

/** 微动响应的是实际音频包络，不根据“正在说话”的布尔状态生成假动作或假口型。 */
test('没有实际口型音频时 speaking 状态不产生说话轻摆', () => {
  const silent = new AvatarPresentation();
  const speaking = new AvatarPresentation();
  const silentOutput = settle(silent, { time: 0.7 });
  const audibleOutput = settle(speaking, { time: 0.7, lip: { open: 0.8, form: 0 } });
  assert.equal(silentOutput.ParamAngleX, 0);
  assert.ok(Math.abs(audibleOutput.ParamAngleX) > 0.5);
  assert.ok(Math.abs(settle(speaking, { time: 0.7 }).ParamAngleX) < 1e-6);
});

/** 换风格仅改变面部，五种手势沿用原时钟；害羞和摇头也不得因自然结束后的同款段补演。 */
test('同回复连续同款在进行中、结束后和风格改变时均只演一次', () => {
  const { expressions } = resourceExpressions();
  for (const gesture of ['nod', 'tilt', 'wave', 'shy', 'shake']) {
    const actual = new AvatarPresentation(expressions);
    const control = new AvatarPresentation(expressions);
    for (let frame = 0; frame <= 220; frame++) {
      const style = frame < 18 ? 'neutral' : frame < 140 ? 'happy' : 'gentle';
      const id = frame < 18 ? 'first' : frame < 140 ? 'second' : 'third';
      assert.deepEqual(
        actual.advance(input({ time: frame / 60, delivery: cue(id, gesture, 'reply-a', style) })),
        control.advance(
          input({ time: frame / 60, delivery: cue('first', gesture, 'reply-a', style) })
        ),
        `${gesture}/${frame}`
      );
    }
  }
});

/** 害羞复用同一短动作生命周期，双臂同步向内且嘴独立，不能因新分句或取消再补演。 */
test('害羞低头收拢双臂，自然结束与取消后回落且新回复可重新动作', () => {
  const runtime = new AvatarPresentation(resourceExpressions().expressions);
  const first = cue('first-shy', 'shy', 'reply-a', 'shy');
  let peak;
  for (let frame = 0; frame <= 180; frame++) {
    const output = runtime.advance(
      input({ time: frame / 60, delivery: first, lip: { open: 0.63, form: -0.4 } })
    );
    assert.equal(output.ParamArmL, output.ParamArmR);
    assert.equal(output.ParamMouthOpenY, 0.63);
    assert.equal(output.ParamMouthForm, -0.4);
    if (!peak || output.ParamArmL < peak.ParamArmL) peak = output;
  }
  assert.ok(peak.ParamArmL < -0.5);
  assert.ok(peak.ParamAngleY < -12);
  assert.ok(peak.ParamAngleX < -4);
  const finished = settle(runtime, { time: 3.5, delivery: first });
  assert.ok(Math.abs(finished.ParamArmL) < 1e-6);
  assert.ok(Math.abs(finished.ParamArmR) < 1e-6);
  const repeated = cue('next-shy', 'shy', 'reply-a', 'shy');
  assert.ok(Math.abs(settle(runtime, { time: 4, delivery: repeated }).ParamArmL) < 1e-6);

  const next = cue('new-shy', 'shy', 'reply-b', 'shy');
  runtime.advance(input({ time: 5, delivery: next }));
  assert.ok(settle(runtime, { time: 5.9, delivery: next }, 30).ParamArmL < -0.5);
  const interrupted = runtime.advance(input({ time: 5.95 }));
  assert.ok(interrupted.ParamArmL < 0, '取消保留阻尼回落而非肢体跳变');
  const closed = settle(runtime, { time: 6.5 });
  assert.ok(Math.abs(closed.ParamArmL) < 1e-6);
  assert.ok(Math.abs(closed.ParamArmR) < 1e-6);
  assert.ok(Math.abs(closed.ParamEyeBallX) < 1e-6);
  assert.ok(Math.abs(closed.ParamEyeBallY) < 1e-6);
  const late = cue('late-shy', 'shy', 'reply-b', 'neutral');
  const cancelled = settle(runtime, { time: 7, delivery: late });
  assert.ok(Math.abs(cancelled.ParamArmL) < 1e-6);
  assert.ok(Math.abs(cancelled.ParamArmR) < 1e-6);
  assert.equal(cancelled.ParamMouthOpenY, 0);
  assert.equal(cancelled.ParamMouthForm, 0);
});

/** 摇头须真正向两边移动且只完成一次往返；取消保留短阻尼尾，不重播，嘴和手臂不参与。 */
test('摇头短曲线覆盖左右，自然结束和取消后回正且不抢口型', () => {
  const runtime = new AvatarPresentation();
  const first = cue('first-shake', 'shake');
  let left = 0;
  let right = 0;
  for (let frame = 0; frame <= 180; frame++) {
    const output = runtime.advance(input({ time: frame / 60, delivery: first }));
    left = Math.min(left, output.ParamAngleX);
    right = Math.max(right, output.ParamAngleX);
    assert.equal(output.ParamArmL, 0);
    assert.equal(output.ParamArmR, 0);
    assert.equal(output.ParamMouthOpenY, 0);
    assert.equal(output.ParamMouthForm, 0);
  }
  assert.ok(left < -12 && right > 12);
  assert.ok(Math.abs(settle(runtime, { time: 3.5, delivery: first }).ParamAngleX) < 1e-6);
  const repeated = cue('same-reply-shake', 'shake');
  assert.ok(Math.abs(settle(runtime, { time: 4, delivery: repeated }).ParamAngleX) < 1e-6);
  const next = cue('new-reply-shake', 'shake', 'reply-b');
  runtime.advance(input({ time: 5, delivery: next }));
  const active = settle(runtime, { time: 5.45, delivery: next }, 30);
  assert.ok(active.ParamAngleX > 12);
  const stopped = runtime.advance(input({ time: 5.5 }));
  assert.ok(stopped.ParamAngleX > 0 && stopped.ParamAngleX < active.ParamAngleX);
  const late = cue('late-shake', 'shake', 'reply-b');
  const cancelled = settle(runtime, { time: 6.5, delivery: late });
  assert.ok(Math.abs(cancelled.ParamAngleX) < 1e-6);
  const speaking = settle(runtime, {
    time: 7,
    delivery: late,
    lip: { open: 0.71, form: 0.25 },
  });
  assert.equal(speaking.ParamMouthOpenY, 0.71);
  assert.equal(speaking.ParamMouthForm, 0.25);
});

/** closed 会撤销当前动作，但同一回复下一段仍是已消费手势，不能把关闭间隔当新回复。 */
test('关闭间隔后同回复新段不补演，新回复仍可使用同款手势', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  let newReplyPeak = 0;
  for (let frame = 0; frame <= 260; frame++) {
    const first = frame < 20 ? cue('first', 'wave') : null;
    const next = frame >= 150 ? cue('next-reply', 'wave', 'reply-b') : null;
    const delivery = next ?? (frame >= 40 ? cue('same-reply-next-segment', 'wave') : first);
    const output = actual.advance(input({ time: frame / 60, delivery }));
    assert.deepEqual(output, control.advance(input({ time: frame / 60, delivery: next ?? first })));
    if (frame >= 150) newReplyPeak = Math.max(newReplyPeak, output.ParamArmL);
  }
  assert.ok(newReplyPeak > 0.65);
});

/** none 不硬切正在进行的动作；完成前又收到同款即取消间隔，旧 none 重发也不能解锁。 */
test('进行中的none与重复同款不重启，只有完成后的新none重新开放', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  let secondPeak = 0;
  for (let frame = 0; frame <= 290; frame++) {
    let delivery = cue('first', 'wave');
    if (frame >= 20) delivery = cue('old-none', 'none');
    if (frame >= 25) delivery = cue('second', 'wave');
    if (frame === 150) delivery = cue('old-none', 'none');
    if (frame > 150) delivery = cue('third', 'wave');
    if (frame === 180) delivery = cue('new-none', 'none');
    if (frame > 180) delivery = cue('after-none', 'wave');
    const expected =
      frame < 180
        ? cue('first', 'wave')
        : frame === 180
          ? cue('new-none', 'none')
          : cue('after-none', 'wave');
    const output = actual.advance(input({ time: frame / 60, delivery }));
    assert.deepEqual(output, control.advance(input({ time: frame / 60, delivery: expected })));
    if (frame > 180) secondPeak = Math.max(secondPeak, output.ParamArmL);
  }
  assert.ok(secondPeak > 0.65);
});

/** 连续安静语义可等待短动作自然结束后开放下一次动作，不要求额外计时器或任意限流。 */
test('none持续到自然完成后允许同款新动作，完成边界同帧也成立', () => {
  for (const noneFrame of [20, 99]) {
    const runtime = new AvatarPresentation();
    let secondPeak = 0;
    for (let frame = 0; frame <= 225; frame++) {
      const delivery =
        frame < noneFrame
          ? cue('first', 'wave')
          : frame < 110
            ? cue('none', 'none')
            : cue('second', 'wave');
      const output = runtime.advance(input({ time: frame / 60, delivery }));
      if (frame >= 110) secondPeak = Math.max(secondPeak, output.ParamArmL);
    }
    assert.ok(secondPeak > 0.65, `none frame ${noneFrame}`);
  }
});

/** 中途取消不是自然完成；即使后来有 none 且时钟越过原时长，也不能复活已取消的同款。 */
test('取消清除待完成none间隔，迟到none和同款新段不能补演', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  for (let frame = 0; frame <= 220; frame++) {
    let delivery = frame < 25 ? cue('first', 'wave') : null;
    if (frame >= 20 && frame < 25) delivery = cue('pending-none', 'none');
    if (frame >= 130) delivery = cue('late-none', 'none');
    if (frame >= 140) delivery = cue('late-wave', 'wave');
    assert.deepEqual(
      actual.advance(input({ time: frame / 60, delivery })),
      control.advance(
        input({ time: frame / 60, delivery: frame < 25 ? cue('first', 'wave') : null })
      )
    );
  }
});

/** 旧段去重先于状态改变，否则旧回复重发会取消新回复；再次取消后旧段也不能自我复活。 */
test('迟到旧cue不抢走新回复动作，取消后旧cue和同回复新段都不恢复', () => {
  const actual = new AvatarPresentation();
  const control = new AvatarPresentation();
  for (let frame = 0; frame <= 190; frame++) {
    const expected =
      frame < 20
        ? cue('old', 'wave')
        : frame < 40 || frame >= 70
          ? null
          : cue('current', 'nod', 'reply-b');
    let delivery = expected;
    if (frame === 55) delivery = cue('old', 'wave');
    if (frame >= 80) delivery = cue('current', 'nod', 'reply-b');
    if (frame >= 90) delivery = cue('current-late', 'nod', 'reply-b');
    assert.deepEqual(
      actual.advance(input({ time: frame / 60, delivery })),
      control.advance(input({ time: frame / 60, delivery: expected }))
    );
  }
});

/** 新协议要求显式回复身份，缺字段不能退回按句段猜回复的旧行为。 */
test('缺少replyKey的调用不启动手势', () => {
  const runtime = new AvatarPresentation();
  const output = settle(runtime, {
    time: 0.8,
    delivery: { id: 'invalid', style: 'neutral', gesture: 'wave' },
  });
  assert.equal(output.ParamArmL, 0);
});

/** 固定相位只隔离主动作包络：次级X/Y在峰值保留30%，端点恢复，嘴和主动作始终原值。 */
test('主手势平滑压低音频轻摆但不改变真实口型和主曲线', () => {
  const action = new AvatarPresentation();
  const silentAction = new AvatarPresentation();
  const control = new AvatarPresentation();
  const delivery = cue('wave', 'wave');
  const lip = { open: 0.63, form: -0.4 };
  for (const [time, weight] of [
    [0, 1],
    [0.4125, 0.65],
    [0.825, 0.3],
    [1.65, 1],
  ]) {
    const plain = settle(control, { time, lip });
    const output = settle(action, { time, delivery, lip });
    const primary = settle(silentAction, { time, delivery });
    for (const id of ['ParamAngleX', 'ParamAngleY']) {
      assert.ok(Math.abs(output[id] - plain[id] * weight) < 1e-6, `${id}/${time}`);
    }
    assert.equal(output.ParamMouthOpenY, lip.open);
    assert.equal(output.ParamMouthForm, lip.form);
    assert.equal(output.ParamArmL, primary.ParamArmL);
    assert.equal(output.ParamAngleZ, primary.ParamAngleZ);
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
    const output = settle(runtime, { delivery: cue(style, 'none', 'reply-a', style) });
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
    assert.ok(Math.abs(output.ParamBrowLAngle) > 0.1);
    assert.ok(Math.abs(output.ParamBrowRAngle) > 0.1);
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
