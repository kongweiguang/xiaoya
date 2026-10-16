import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { AvatarPresentation } from '../lib/avatar/presentation.ts';

/** 固定帧时钟与视线，只有真实生产状态机处理许可，不在测试实现第二个动画调度器。 */
function input(time, delivery, lip = null) {
  return {
    time,
    delta: 1 / 60,
    behavior: delivery.phase,
    delivery,
    gaze: { x: 0, y: 0 },
    blink: 1,
    lip,
  };
}

/** 给每个执行稳定身份，视觉修订独立变化，才能分别检查撤权与重复消息。 */
function cue(id, overrides = {}) {
  return {
    id,
    instance: 'job-a',
    turnId: 'turn-a',
    replyKey: '["job-a",""]',
    segmentKey: '',
    phase: 'listening',
    style: 'curious',
    gesture: 'nod',
    gestureId: 'turn-a:listen:nod',
    gestureSource: 'dialogue',
    energy: 0.5,
    ...overrides,
  };
}

/** 保留轮次、阶段和神态，仅移除主动作许可，对应04应用在VAD停声或ASR final的输出。 */
function revoked(current, id = 'current-revoked') {
  return {
    ...current,
    id,
    gesture: 'none',
    gestureId: '',
    gestureSource: 'none',
  };
}

/** 已准备但未发力只报告取消，不能伪造started或消耗冷却，也不能停声后凭旧候选发力。 */
test('倾听准备未满120ms撤权后永不发力、不计自然预算且保留神态', () => {
  const actual = new AvatarPresentation(),
    baseline = new AvatarPresentation();
  const action = cue('stable-partial'),
    none = revoked(action);
  let last, reference;
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60;
    if (frame === 4) assert.equal(actual.activeGesture.stroke, null);
    last = actual.advance(input(time, frame < 4 ? action : none));
    reference = baseline.advance(input(time, none));
    if (frame >= 4) {
      assert.equal(
        actual.activeGesture,
        null,
        `frame=${frame} obsolete listening candidate survived`
      );
      assert.deepEqual(actual.naturalStarts, []);
    }
  }
  assert.equal(actual.selectedExpression, 'curious');
  assert.deepEqual(
    actual.drainAcknowledgments().map((ack) => [ack.gesture_id, ack.status]),
    [[action.gestureId, 'canceled']]
  );
  for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ'])
    assert.ok(Math.abs(last[id] - reference[id]) < 1e-6, id);
});

/** 已发力的取消只撤执行许可，既有3秒/6秒/10秒预算不回滚；主轴通过原阻尼平滑收回。 */
test('倾听已发力撤权立即清执行、平滑收回并保留已开始预算', () => {
  const actual = new AvatarPresentation(),
    baseline = new AvatarPresentation();
  const action = cue('stable-partial'),
    none = revoked(action);
  const tooSoon = cue('new-tilt', {
    gesture: 'tilt',
    gestureId: 'turn-a:listen:tilt',
  });
  let previous, last, reference, startedBudget;
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60;
    if (frame === 12) {
      assert.equal(actual.activeGesture.stroke, 8 / 60);
      startedBudget = [...actual.naturalStarts];
    }
    const delivery = frame < 12 ? action : frame >= 60 && frame < 90 ? tooSoon : none;
    last = actual.advance(input(time, delivery));
    reference = baseline.advance(input(time, none));
    if (frame >= 12) {
      assert.equal(
        actual.activeGesture,
        null,
        `frame=${frame} revoked or cooling action is active`
      );
      assert.deepEqual(actual.naturalStarts, startedBudget);
      for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ'])
        assert.ok(Math.abs(last[id] - previous[id]) < 2, `${id} lost existing smoothness gate`);
    }
    assert.equal(last.ParamMouthOpenY, 0);
    assert.equal(last.ParamMouthForm, 0);
    previous = last;
  }
  assert.equal(actual.lastNatural.get('nod'), 8 / 60);
  assert.equal(actual.lastNatural.has('tilt'), false);
  for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ'])
    assert.ok(Math.abs(last[id] - reference[id]) < 1e-6, id);
});

/** 同片段自然说话的none可能只是表情更新，不能将倾听许可规则扩大成中断说话动作。 */
test('说话同片段none更新保留已开始自然动作与真实口型', () => {
  const actual = new AvatarPresentation(),
    control = new AvatarPresentation();
  const action = cue('speaking-action', {
    phase: 'speaking',
    segmentKey: 'segment-a',
  });
  for (let frame = 0; frame <= 180; frame++) {
    const time = frame / 60,
      audio = { at: time, energy: 0.6, onset: frame === 8 ? 0.5 : 0 };
    const original = { ...action, audio };
    const delivery = frame >= 20 ? revoked(original, 'expression-only') : original;
    const lip = { open: 0.54, form: -0.25 };
    const observed = actual.advance(input(time, delivery, lip));
    const reference = control.advance(input(time, original, lip));
    assert.deepEqual(observed, reference);
    if (frame >= 20 && frame < 45)
      assert.equal(actual.activeGesture.delivery.gestureId, action.gestureId);
    assert.equal(observed.ParamMouthOpenY, lip.open);
    assert.equal(observed.ParamMouthForm, lip.form);
  }
  assert.equal(actual.naturalStarts.length, 1);
});

/** 指令仍由自己的执行身份和回执推进，语音phase变化不能误借倾听撤权规则取消它。 */
test('无声指令跨thinking/speaking/idle仍只执行一次并完成回执', () => {
  const actual = new AvatarPresentation();
  const action = cue('command', {
    gesture: 'wave',
    gestureId: 'command-a',
    gestureSource: 'command',
  });
  for (let frame = 0; frame <= 180; frame++) {
    const phase = frame < 20 ? 'thinking' : frame < 50 ? 'speaking' : 'idle';
    actual.advance(input(frame / 60, { ...action, id: `phase-${phase}`, phase }));
  }
  assert.deepEqual(
    actual.drainAcknowledgments().map((ack) => [ack.gesture_id, ack.status]),
    [
      ['command-a', 'started'],
      ['command-a', 'completed'],
    ]
  );
  assert.deepEqual(actual.naturalStarts, []);
});

/** 已看过的旧none在身份去重后必须直接退出，不能回头取消其后才获准的新动作。 */
test('同轮重复旧none身份不能取消新自然动作或独立指令', async (context) => {
  for (const source of ['dialogue', 'command'])
    await context.test(source, () => {
      const actual = new AvatarPresentation();
      const action = cue('new-action', {
        gestureSource: source,
        gestureId: `new-${source}`,
      });
      const stale = revoked(action, 'old-revocation');
      for (let frame = 0; frame <= 15; frame++) {
        const delivery = frame === 0 || frame === 4 ? stale : action;
        actual.advance(input(frame / 60, delivery));
        if (frame === 4) assert.equal(actual.activeGesture.delivery.gestureId, action.gestureId);
      }
      assert.notEqual(actual.activeGesture.stroke, null);
    });
});

/** 真实运输门先检查实例和单调revision，动画类无需从不透明id猜测消息先后顺序。 */
test('DeliveryGate拒绝迟到旧轮次none和异实例消息，新自然动作与指令保持许可', () => {
  const gate = new DeliveryGate(),
    actual = new AvatarPresentation();
  const stale = {
    v: 3,
    instance: 'job-a',
    revision: 1,
    reply_id: '',
    segment_id: '',
    state: 'active',
    style: 'curious',
    gesture: 'none',
    phase: 'listening',
    turn_id: 'turn-old',
    gesture_id: '',
    gesture_source: 'none',
    command_feedback: 'none',
    energy: 0.5,
  };
  gate.snapshot(stale);
  const current = {
    ...stale,
    revision: 2,
    turn_id: 'turn-new',
    gesture: 'nod',
    gesture_id: 'new-natural',
    gesture_source: 'dialogue',
  };
  gate.receive(current);
  for (let frame = 0; frame <= 15; frame++) {
    if (frame === 4) {
      gate.receive(stale);
      gate.receive({ ...stale, instance: 'foreign', revision: 999 });
    }
    const delivery = gate.read(false, true);
    assert.equal(delivery.gestureId, 'new-natural');
    actual.advance(input(frame / 60, delivery));
  }
  assert.notEqual(actual.activeGesture.stroke, null);
  gate.receive({
    ...current,
    revision: 3,
    gesture: 'wave',
    gesture_id: 'new-command',
    gesture_source: 'command',
    phase: 'thinking',
  });
  gate.receive(stale);
  gate.receive({
    ...current,
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
  });
  actual.advance(input(16 / 60, gate.read(false, true)));
  assert.equal(actual.activeGesture.delivery.gestureId, 'new-command');
});
