import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  AvatarPresentation,
  isAvatarExpressionName,
  parseAvatarExpression,
} from '../lib/avatar/presentation.ts';

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

/** Add 必须相对基准而不是前一帧，长句中的眉毛不能一路漂移到极限。 */
test('表情资源真实应用但数百帧不累计参数', () => {
  const runtime = new AvatarPresentation({
    happy: expression([{ Id: 'ParamBrowLY', Value: 0.3, Blend: 'Add' }]),
  });
  const delivery = { id: 'speech-1', style: 'happy', gesture: 'none' };
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
    delivery: { id: 'speech-1', style: 'happy', gesture: 'wave' },
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
      const state = input({ delivery: { id: style, style, gesture: 'none' } });
      assert.deepEqual(left.advance(state), right.advance(state));
    }
  }
});

/** 一句话只能启动一次短动作；迟到重复 cue、静音恢复或过期帧都不能续演旧动作。 */
test('短手势只启动一次，结束、打断、重发后均回中性', () => {
  const runtime = new AvatarPresentation();
  const delivery = { id: 'speech-1', style: 'neutral', gesture: 'wave' };
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

  const replacement = { id: 'speech-2', style: 'neutral', gesture: 'wave' };
  runtime.advance(input({ time: 6, delivery: replacement }));
  assert.ok(settle(runtime, { time: 6.8, delivery: replacement }, 20).ParamArmL > 0.5);
  const firstStop = runtime.advance(input({ time: 6.9 }));
  assert.ok(firstStop.ParamArmL > 0, '取消平滑收尾而非生硬跳回');
  assert.ok(settle(runtime, { time: 7 }).ParamArmL < 1e-6);
});

/** 主动作互斥；新句的点头不能继续上一句挥手，后台长暂停也不应补播动作队列。 */
test('新动作替换旧动作，长帧间隔不补播且数值有限', () => {
  const runtime = new AvatarPresentation();
  const wave = { id: 'wave', style: 'neutral', gesture: 'wave' };
  runtime.advance(input({ delivery: wave }));
  settle(runtime, { time: 0.8, delivery: wave });
  const nod = { id: 'nod', style: 'neutral', gesture: 'nod' };
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
