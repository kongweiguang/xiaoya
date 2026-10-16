import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { cleanup, production, renderHook } from './helpers/dom.mjs';

afterEach(cleanup);

const instance = 'aa1a79436e1a41718d95cbb169634a34';
const turn = '2b8b897da47141f3a792fb3a39dd05d9';
const reply = 'speech_a73838daa61c';
const segments = [
  'SG_1450534af99f4a47bc958d7e90b0aab2',
  'SG_8dccff9843ef4870b625cc447d5f3a99',
  'SG_db26649c6ea94d6eb6be1dbf5db08806',
  'SG_100f4f41126044c1ba2a8cd28ce507ea',
];

/** 保留22真实失败的四元组与版本；测试只重放门控事件，不伪造录音或模型输出。 */
function state(revision, segment = '', overrides = {}) {
  return {
    v: 3,
    instance,
    revision,
    turn_id: turn,
    reply_id: segment ? reply : '',
    segment_id: segment,
    state: 'active',
    phase: segment ? 'speaking' : 'thinking',
    style: 'neutral',
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
    command_feedback: 'none',
    energy: 0.495,
    ...overrides,
  };
}

/** 字幕仍走生产精确身份核验，不能用上一句文本为工具续说授权。 */
function message(value) {
  return {
    id: value.segment_id,
    message: '二加二等于四，不是五哦。',
    from: { identity: 'agent-a', isLocal: false },
    attributes: {
      'xiaoya.delivery': JSON.stringify(value),
      'lk.segment_id': value.segment_id,
    },
  };
}

/** 用真实React effect复现SDK先暂停、可靠thinking随后到达，避免只测试一个新Gate接口。 */
async function mountedGate(initial) {
  const { useAvatarDelivery } = await production('hooks/use-avatar-delivery.ts');
  const gate = new DeliveryGate();
  gate.snapshot(state(55, '', { state: 'closed', phase: 'idle' }));
  gate.receive(initial);
  const input = {
    gate: { current: gate },
    messages: [message(initial)],
    identity: 'agent-a',
    state: 'speaking',
    track: {},
  };
  const view = renderHook(
    (props) =>
      useAvatarDelivery(props.gate, props.messages, props.identity, props.state, props.track),
    { initialProps: input }
  );
  return { gate, input, view };
}

/** 22原轨迹第三句有100个强PCM窗却嘴0；两次工具暂停都只能撤旧段，续段必须重新等自己的PCM。 */
test('22真实工具续说轨迹在同reply新段恢复口型许可，连续暂停不封整reply', async () => {
  const { gate, input, view } = await mountedGate(state(58, segments[0]));
  assert.ok(gate.read(true, true));
  gate.receive(state(59, segments[0]));
  gate.receive(state(60, '', { state: 'closed', phase: 'idle' }));
  const second = state(61, segments[1]);
  gate.receive(second);
  view.rerender({ ...input, messages: [message(second)] });
  assert.ok(gate.read(true, true));
  // SDK的工具thinking可先于可靠62到达，正是整reply误封的触发边沿。
  view.rerender({ ...input, messages: [message(second)], state: 'thinking' });
  assert.equal(gate.read(false, false), null);
  assert.equal(gate.audibleSpeechAuthorized, false);
  gate.receive(state(62));
  gate.receive(state(63));
  const third = state(64, segments[2]);
  gate.receive(third);
  view.rerender({ ...input, messages: [message(third)] });
  assert.equal(gate.read(true, false), null);
  assert.equal(gate.read(false, true), null);
  assert.equal(gate.read(true, true)?.segmentKey, JSON.stringify([instance, reply, segments[2]]));
  assert.equal(gate.audibleSpeechAuthorized, true);
  gate.receive(state(65, '', { state: 'closed', phase: 'idle' }));
  const fourth = state(66, segments[3]);
  gate.receive(fourth);
  view.rerender({ ...input, messages: [message(fourth)] });
  assert.ok(gate.read(true, true));
  view.rerender({ ...input, messages: [message(fourth)], state: 'thinking' });
  gate.receive(state(68));
  const fifth = state(69, 'tool-continuation-2');
  gate.receive(fifth);
  view.rerender({ ...input, messages: [message(fifth)] });
  assert.equal(gate.read(true, false), null);
  assert.ok(gate.read(true, true));
});

/** 新可靠句段可能比SDK状态回调早；尚未出声的候选不能被迟到thinking封为旧段。 */
test('新段先到和SDK迟到暂停不消费待播段，自身字幕及PCM仍必需', async () => {
  const first = state(61, segments[1]);
  const { gate, input, view } = await mountedGate(first);
  assert.ok(gate.read(true, true));
  gate.receive(state(62));
  const next = state(64, segments[2]);
  gate.receive(next);
  view.rerender({ ...input, messages: [message(first)], state: 'thinking' });
  assert.equal(gate.read(true, true), null, '旧字幕不能借给新段');
  view.rerender({ ...input, messages: [message(next)], state: 'thinking' });
  assert.equal(gate.read(false, true), null);
  assert.equal(gate.read(true, false), null);
  view.rerender({ ...input, messages: [message(next)] });
  assert.ok(gate.read(true, true));
});

/** 更高修订只更新语义，不提供旧段重播权；真打断和播放失败仍须覆盖整轮或整回复。 */
test('工具续说不解封高修订旧段，也不穿过用户打断或播放能力撤权', () => {
  for (const revoke of ['suspendSpeaking', 'interrupt', 'invalidatePlayback']) {
    const gate = new DeliveryGate();
    const first = state(61, segments[1]);
    gate.snapshot(state(55, '', { state: 'closed', phase: 'idle' }));
    gate.receive(first);
    gate.offer([message(first)], 'agent-a');
    assert.ok(gate.read(true, true));
    gate[revoke]();
    gate.receive(state(62));
    const late = state(63, segments[1]);
    gate.receive(late);
    gate.offer([message(late)], 'agent-a');
    assert.equal(gate.read(true, true), null, `${revoke}: 高修订旧段不得复活`);
    const next = state(64, segments[2]);
    gate.receive(next);
    gate.offer([message(next)], 'agent-a');
    assert.equal(Boolean(gate.read(true, true)), revoke === 'suspendSpeaking');
    if (revoke === 'interrupt') {
      gate.receive(state(65, 'same-reply-new-turn', { turn_id: 'other-turn' }));
      gate.offer([message(gate.diagnostics.state)], 'agent-a');
      assert.equal(gate.read(true, true), null, '已取消reply不能借新turn清除墓碑');
    }
  }
});

/** 工具暂停时current没有reply字段，真实能力故障仍须封住本轮已播回复而不是丢失撤权对象。 */
test('工具thinking期间播放能力失效仍封本轮旧reply，新turn的新reply可以恢复', () => {
  const gate = new DeliveryGate();
  const first = state(61, segments[1]);
  gate.snapshot(first);
  gate.offer([message(first)], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.suspendSpeaking();
  gate.receive(state(62));
  gate.invalidatePlayback();
  const continuation = state(64, segments[2]);
  gate.receive(continuation);
  gate.offer([message(continuation)], 'agent-a');
  assert.equal(gate.read(true, true), null);
  const next = state(65, 'fresh-segment', { turn_id: 'fresh-turn', reply_id: 'fresh-reply' });
  gate.receive(next);
  gate.offer([message(next)], 'agent-a');
  assert.ok(gate.read(true, true));
});

/** 已撤旧动作只可报告真实取消；暂停不合成新动作，续段必须在当前身份下独立started。 */
test('暂停闭环旧自然started且拒迟到completed，续段回执仍受当前段守门', () => {
  const gate = new DeliveryGate();
  const acknowledgments = [];
  gate.setAcknowledgmentSink((ack) => acknowledgments.push(ack));
  const first = state(61, segments[1], {
    gesture: 'nod',
    gesture_id: 'old-nod',
    gesture_source: 'dialogue',
  });
  gate.snapshot(state(55, '', { state: 'closed', phase: 'idle' }));
  gate.receive(first);
  gate.offer([message(first)], 'agent-a');
  assert.ok(gate.read(true, true));
  const ack = { instance, turn_id: turn, gesture_id: 'old-nod', stroke_started: true };
  gate.acknowledge({ ...ack, status: 'started' });
  gate.suspendSpeaking();
  gate.suspendSpeaking();
  gate.acknowledge({ ...ack, status: 'completed' });
  gate.acknowledge({ ...ack, status: 'started' });
  assert.deepEqual(
    acknowledgments.map((value) => value.status),
    ['started', 'canceled']
  );
  const next = state(64, segments[2], {
    gesture: 'nod',
    gesture_id: 'new-nod',
    gesture_source: 'dialogue',
  });
  gate.receive(state(62));
  gate.receive(next);
  gate.offer([message(next)], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.acknowledge({ ...ack, gesture_id: 'new-nod', status: 'started' });
  assert.equal(acknowledgments.at(-1).gesture_id, 'new-nod');
});

/** 连接恢复仍是硬边界；同reply的新段不能靠暂停记录或跨轮字幕绕过快照防重播。 */
test('重连不补演工具续说，字幕turn不匹配也不授权新段', () => {
  const gate = new DeliveryGate();
  const first = state(61, segments[1]);
  gate.snapshot(first);
  gate.offer([message(first)], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.suspendSpeaking();
  const resumed = new DeliveryGate(gate);
  const next = state(64, segments[2]);
  resumed.snapshot(next);
  resumed.offer([message(next)], 'agent-a');
  assert.equal(resumed.read(true, true), null);
  const fresh = new DeliveryGate();
  fresh.snapshot(next);
  fresh.offer([message({ ...next, turn_id: 'wrong-turn' })], 'agent-a');
  assert.equal(fresh.read(true, true), null);
  fresh.offer([message(next)], 'agent-a');
  assert.ok(fresh.read(true, true));
});

/** 有界清理只能保守降级，不能删除旧段后放行重播；实例换代才清掉该实例的墓碑。 */
test('句段墓碑有界且容量回收不复活旧段，新实例不继承旧身份', () => {
  const gate = new DeliveryGate();
  const first = state(61, 'segment-0', { reply_id: 'reply-0' });
  gate.snapshot(first);
  for (let index = 1; index <= 70; index++)
    gate.receive(state(61 + index, `segment-${index}`, { reply_id: `reply-${index}` }));
  assert.equal(gate.suspendedSegments.size, 64);
  const stale = { ...first, revision: 132 };
  gate.receive(stale);
  gate.offer([message(stale)], 'agent-a');
  assert.equal(gate.read(true, true), null);
  const restarted = new DeliveryGate(gate);
  const newInstance = { ...first, revision: 1, instance: 'new-instance' };
  restarted.snapshot(newInstance);
  restarted.offer([message(newInstance)], 'agent-a');
  assert.ok(restarted.read(true, true));
  assert.equal(restarted.suspendedSegments.size, 0);
});
