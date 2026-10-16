import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { AvatarPresentation } from '../lib/avatar/presentation.ts';

/** 同一轮的实时倾听许可用于真实准备/撤销，不能从快照或测试直接制造已执行事实。 */
function live() {
  const state = {
    v: 3,
    instance: 'job',
    revision: 1,
    state: 'active',
    turn_id: 'turn',
    reply_id: '',
    segment_id: '',
    phase: 'listening',
    style: 'happy',
    gesture: 'nod',
    gesture_id: 'turn:listen:nod',
    gesture_source: 'dialogue',
    command_feedback: 'none',
    energy: 0.5,
  };
  const gate = new DeliveryGate(),
    received = [],
    runtime = new AvatarPresentation();
  gate.setAcknowledgmentSink((event) => received.push(event));
  gate.snapshot({
    ...state,
    revision: 0,
    state: 'closed',
    phase: 'idle',
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
  });
  gate.receive(state);
  return { state, gate, received, runtime };
}

/** 原生产帧入口决定stroke，嘴部仍无PCM即闭，测试不手改动作或回执内部状态。 */
function frame(view, time) {
  const delivery = view.gate.read(false, false);
  const pose = view.runtime.advance({
    time,
    delta: 1 / 60,
    behavior: delivery?.phase ?? 'idle',
    delivery,
    gaze: { x: 0, y: 0 },
    blink: 1,
    lip: null,
  });
  assert.equal(pose.ParamMouthOpenY, 0);
  return view.runtime.drainAcknowledgments();
}

/** 与MIC17的118.3ms撤权相同，原120ms准备尚未发力时只能产生显式false取消。 */
test('倾听准备期间撤权准确报告stroke_started=false，既不假started也不漏终态', () => {
  const view = live();
  assert.deepEqual(frame(view, 31.4261), []);
  view.gate.receive({
    ...view.state,
    revision: 2,
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
  });
  const events = frame(view, 31.5444);
  assert.deepEqual(
    events.map((event) => [event.status, event.stroke_started]),
    [['canceled', false]]
  );
  for (const event of events) view.gate.acknowledge(event);
  assert.deepEqual(view.received, events);
  assert.deepEqual(frame(view, 50), []);
});

/** started即使丢在网络前，renderer仍记得真实stroke，终态不得因Gate没有started而猜false。 */
test('丢失started事件之后取消仍报告true，不以缺ACK释放服务端未知预算', () => {
  const view = live();
  frame(view, 0);
  const dropped = frame(view, 0.13);
  assert.deepEqual(
    dropped.map((event) => [event.status, event.stroke_started]),
    [['started', true]]
  );
  view.gate.receive({
    ...view.state,
    revision: 2,
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
  });
  const terminal = frame(view, 0.15);
  assert.deepEqual(
    terminal.map((event) => [event.status, event.stroke_started]),
    [['canceled', true]]
  );
  for (const event of terminal) view.gate.acknowledge(event);
  assert.deepEqual(view.received, terminal);
});

/** 隐藏等Gate失效入口只掌握已started事实，必须合成true而不是把未转发等同未执行。 */
test('后台撤权只为已真实started动作附true，未开始不会由Gate合成false', () => {
  for (const started of [false, true]) {
    const view = live();
    frame(view, 0);
    if (started) for (const event of frame(view, 0.13)) view.gate.acknowledge(event);
    view.gate.invalidatePresentation();
    assert.deepEqual(
      view.received.map((event) => [event.status, event.stroke_started]),
      started
        ? [
            ['started', true],
            ['canceled', true],
          ]
        : []
    );
  }
});

/** 终态与身份守门不因新事实放宽，旧轮未开始、非法布尔及矛盾完成都不得发送。 */
test('stroke事实仍受实时身份/终态/类型门限制', () => {
  const view = live();
  const ack = {
    instance: view.state.instance,
    turn_id: view.state.turn_id,
    gesture_id: view.state.gesture_id,
  };
  for (const stroke_started of [0, 1, 'false', null])
    view.gate.acknowledge({ ...ack, status: 'canceled', stroke_started });
  view.gate.acknowledge({ ...ack, status: 'started', stroke_started: false });
  view.gate.acknowledge({ ...ack, status: 'completed', stroke_started: false });
  assert.deepEqual(view.received, []);
  view.gate.acknowledge({ ...ack, status: 'canceled', stroke_started: false });
  view.gate.acknowledge({ ...ack, status: 'started', stroke_started: true });
  assert.deepEqual(
    view.received.map((event) => [event.status, event.stroke_started]),
    [['canceled', false]]
  );
  const old = live();
  old.gate.receive({ ...old.state, revision: 2, turn_id: 'next', gesture_id: 'next:nod' });
  old.gate.acknowledge({ ...ack, status: 'canceled', stroke_started: false });
  assert.deepEqual(old.received, []);
});
