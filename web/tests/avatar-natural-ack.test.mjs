import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { AvatarPresentation } from '../lib/avatar/presentation.ts';

/** 用真实v3身份建立许可，快照与实时到达严格分开，避免测试替生产Gate补造执行记录。 */
function state(revision, overrides = {}) {
  return {
    v: 3,
    instance: 'job',
    revision,
    state: 'active',
    turn_id: 'turn',
    reply_id: 'reply',
    segment_id: 'segment',
    phase: 'speaking',
    style: 'curious',
    gesture: 'tilt',
    gesture_id: 'natural',
    gesture_source: 'dialogue',
    command_feedback: 'none',
    energy: 0.5,
    ...overrides,
  };
}

/** 只有实时消息可登记执行，历史快照仅作所有者基线，不能触发补演或回执。 */
function live(current = state(1)) {
  const gate = new DeliveryGate(),
    received = [];
  gate.setAcknowledgmentSink((ack) => received.push(ack));
  gate.snapshot(
    state(0, {
      state: 'closed',
      phase: 'idle',
      gesture: 'none',
      gesture_id: '',
      gesture_source: 'none',
    })
  );
  gate.receive(current);
  return { gate, received };
}

/** 缺可选stroke事实仍代表未知；真实renderer预期显式传true，不能用新字段绕过身份门。 */
function ack(current, status, strokeStarted) {
  return {
    instance: current.instance,
    turn_id: current.turn_id,
    gesture_id: current.gesture_id,
    status,
    ...(strokeStarted === undefined ? {} : { stroke_started: strokeStarted }),
  };
}

/** 字幕只授权本句音频；模拟帧输入不直接修改姿态或伪造真实渲染回调。 */
function animate(runtime, time, delivery, audible = true) {
  const speaking = delivery?.phase === 'speaking';
  return runtime.advance({
    time,
    delta: 1 / 60,
    behavior: delivery?.phase ?? 'idle',
    delivery:
      delivery && speaking
        ? { ...delivery, audio: { at: time, energy: audible ? 0.5 : 0, onset: 0 } }
        : delivery,
    gaze: { x: 0, y: 0 },
    blink: 1,
    lip: speaking && audible ? { open: 0.6, form: -0.1 } : null,
  });
}

/** 自然动作和指令共享真实stroke生命周期，准备和静音均不能报告started；不改任何动作曲线。 */
test('五自然动作只在原准备与真实PCM许可后报告一次started/completed', () => {
  for (const phase of ['listening', 'speaking'])
    for (const gesture of ['nod', 'tilt', 'wave', 'shy', 'shake']) {
      const current = state(1, { phase, gesture, gesture_id: `${phase}:${gesture}` });
      const { gate } = live(current),
        runtime = new AvatarPresentation(),
        events = [];
      gate.offer(
        [
          {
            id: 'subtitle',
            message: '当前句',
            from: { identity: 'agent' },
            attributes: {
              'xiaoya.delivery': JSON.stringify(current),
              'lk.segment_id': current.segment_id,
            },
          },
        ],
        'agent'
      );
      for (let frame = 0; frame <= 180; frame++) {
        const time = frame / 60,
          audible = phase !== 'speaking' || time >= 0.3;
        const pose = animate(runtime, time, gate.read(true, audible), audible);
        events.push(...runtime.drainAcknowledgments().map((event) => ({ event, at: time })));
        if (time < (phase === 'speaking' ? 0.3 : 0.12)) assert.equal(events.length, 0);
        assert.equal(pose.ParamMouthOpenY, phase === 'speaking' && audible ? 0.6 : 0);
      }
      assert.deepEqual(
        events.map(({ event }) => event),
        [ack(current, 'started', true), ack(current, 'completed', true)]
      );
      assert.ok(events[0].at >= (phase === 'speaking' ? 0.42 : 0.12));
    }
});

/** 后端只能信任实际已开始记录；重复、未知及未开始的完成都不能由回执队列合成。 */
test('实时自然身份回执幂等，快照与未知身份无回执且自然不改变重试指令', () => {
  const current = state(1),
    { gate, received } = live(current);
  gate.acknowledge(ack(current, 'completed'));
  gate.acknowledge({ ...ack(current, 'started'), gesture_id: 'unknown' });
  gate.acknowledge({ ...ack(current, 'started'), instance: 'foreign' });
  for (const status of ['started', 'started', 'completed', 'canceled'])
    gate.acknowledge(ack(current, status));
  assert.deepEqual(received, [ack(current, 'started'), ack(current, 'completed')]);
  assert.deepEqual(gate.getFeedback(), { feedback: 'none', gesture: null });
  const restored = new DeliveryGate(),
    replayed = [];
  restored.setAcknowledgmentSink((event) => replayed.push(event));
  restored.snapshot(current);
  restored.receive({ ...current, revision: 2 });
  restored.acknowledge(ack(current, 'started'));
  assert.deepEqual(replayed, []);
});

/** 新轮不能采纳迟到started；已开始旧身份终态仅供清理，不得影响当前动作或重试记录。 */
test('旧轮started拒绝，只有曾实际started的旧轮终态可清理', () => {
  const old = state(1),
    { gate, received } = live(old);
  const unstarted = { ...old, revision: 2, gesture_id: 'unstarted' };
  gate.acknowledge(ack(old, 'started'));
  gate.receive(unstarted);
  const next = state(3, { turn_id: 'new-turn', gesture_id: 'new-natural' });
  gate.receive(next);
  gate.acknowledge(ack(unstarted, 'started'));
  gate.acknowledge(ack(unstarted, 'canceled'));
  gate.acknowledge(ack(old, 'completed'));
  gate.acknowledge(ack(next, 'started'));
  assert.deepEqual(received, [ack(old, 'started'), ack(old, 'completed'), ack(next, 'started')]);
});

/** 后台会暂停rAF，Gate必须先撤实际started，防止恢复时finishGesture先于accept而错误报完成。 */
test('隐藏/减少动态/故障原失效入口取消已开始自然动作，恢复迟到completed不复活', () => {
  for (const invalidate of ['interrupt', 'invalidatePresentation']) {
    const current = state(1, { phase: 'listening' }),
      { gate, received } = live(current);
    const runtime = new AvatarPresentation();
    for (let frame = 0; frame <= 20; frame++) {
      animate(runtime, frame / 60, gate.read(false, false));
      for (const event of runtime.drainAcknowledgments()) gate.acknowledge(event);
    }
    assert.deepEqual(received, [ack(current, 'started', true)]);
    gate[invalidate]();
    animate(runtime, 5, gate.read(false, false));
    for (const event of runtime.drainAcknowledgments()) gate.acknowledge(event);
    assert.deepEqual(received, [ack(current, 'started', true), ack(current, 'canceled', true)]);
  }
});

/** 播放撤权只终止原说话动作，迟到本地起声不能取消服务端已发布的新倾听动作。 */
test('播放失效取消speaking自然动作而不取消当前listening', () => {
  const speaking = state(1),
    { gate, received } = live(speaking);
  gate.acknowledge(ack(speaking, 'started'));
  gate.invalidatePlayback();
  gate.acknowledge(ack(speaking, 'completed'));
  const listening = state(2, {
    phase: 'listening',
    turn_id: 'listen-turn',
    gesture_id: 'listen-nod',
  });
  gate.receive(listening);
  gate.acknowledge(ack(listening, 'started'));
  gate.interruptForSpeech();
  gate.acknowledge(ack(listening, 'completed'));
  assert.deepEqual(received, [
    ack(speaking, 'started'),
    ack(speaking, 'canceled', true),
    ack(listening, 'started'),
    ack(listening, 'completed'),
  ]);
});

/** 同轮仍可能切换语音片段；旧句的迟到开始与已撤销倾听许可不能借当前轮次通行。 */
test('同轮换段和倾听none撤权拒绝迟到started，当前自然执行仍可回执', () => {
  const old = state(1),
    { gate, received } = live(old);
  const next = state(2, { segment_id: 'next-segment', gesture_id: 'next-natural' });
  gate.receive(next);
  gate.acknowledge(ack(old, 'started'));
  gate.acknowledge(ack(next, 'started'));
  gate.acknowledge(ack(next, 'completed'));
  const listening = state(3, { phase: 'listening', gesture_id: 'not-started-listen' });
  gate.receive(listening);
  gate.receive({
    ...listening,
    revision: 4,
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
  });
  gate.acknowledge(ack(listening, 'started'));
  assert.deepEqual(received, [ack(next, 'started'), ack(next, 'completed')]);
});

/** 自然回执只服务节奏账本，不能占据用户指令的重试入口或跨连接继承执行许可。 */
test('自然动作不覆盖本轮已知指令，连接换代和64条目录以外身份不得补回执', () => {
  const command = state(1, { gesture_source: 'command', gesture: 'wave', gesture_id: 'explicit' });
  const { gate, received } = live(command);
  const natural = state(2);
  gate.receive(natural);
  gate.acknowledge(ack(natural, 'started'));
  gate.acknowledge(ack(natural, 'completed'));
  gate.receive(
    state(3, { gesture: 'none', gesture_id: '', gesture_source: 'none', command_feedback: 'retry' })
  );
  assert.deepEqual(gate.getFeedback(), { feedback: 'retry', gesture: 'wave' });
  const replacement = new DeliveryGate(gate),
    reconnected = [];
  replacement.setAcknowledgmentSink((event) => reconnected.push(event));
  replacement.snapshot(state(4));
  replacement.acknowledge(ack(natural, 'started'));
  assert.deepEqual(reconnected, []);
  for (let revision = 5; revision < 75; revision++)
    gate.receive(state(revision, { gesture_id: `bounded-${revision}` }));
  gate.acknowledge(ack(command, 'started'));
  assert.deepEqual(received, [ack(natural, 'started'), ack(natural, 'completed')]);
});
