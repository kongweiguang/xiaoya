import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const compiled = await build({
  entryPoints: [fileURLToPath(new URL('../lib/avatar/delivery.ts', import.meta.url))],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node',
});
const { DeliveryGate, parseDelivery, groupReplyMessages } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`
);

/** 固定实例与单调序号只描述协议边界，测试不伪造实际播放完成。 */
function state(revision, overrides = {}) {
  return {
    v: 3,
    instance: 'job-a',
    revision,
    reply_id: 'reply-a',
    segment_id: `segment-${revision}`,
    state: 'active',
    style: 'happy',
    gesture: overrides.state === 'closed' ? 'none' : 'nod',
    phase: overrides.state === 'closed' ? 'idle' : 'speaking',
    turn_id: overrides.turn_id || overrides.reply_id || 'turn-a',
    gesture_id:
      overrides.state === 'closed' || overrides.gesture === 'none' ? '' : `gesture-${revision}`,
    gesture_source:
      overrides.state === 'closed' || overrides.gesture === 'none' ? 'none' : 'dialogue',
    command_feedback: 'none',
    energy: 0.55,
    ...overrides,
  };
}
/** 字幕头与真实身份必须同时匹配，便于验证未知迟到段的拒绝。 */
function message(delivery, overrides = {}) {
  return {
    id: delivery.segment_id,
    timestamp: 1,
    message: '你好。',
    from: { identity: 'agent-a', isLocal: false },
    attributes: {
      'xiaoya.delivery': JSON.stringify(delivery),
      'lk.segment_id': delivery.segment_id,
    },
    ...overrides,
  };
}

test('订阅中的新状态不能被迟到快照覆盖，首次动作还要等可听音频', () => {
  const gate = new DeliveryGate();
  gate.receive(state(3));
  gate.snapshot(state(1));
  gate.offer([message(state(3))], 'agent-a');
  assert.equal(gate.read(true, false), null);
  assert.equal(gate.read(false, true), null);
  assert.equal(gate.read(true, true)?.id, 'job-a:3:segment-3');
  const active = gate.read(true, false);
  assert.equal(active.gesture, 'nod');
  gate.receive(state(2));
  assert.equal(gate.read(true, true), active);
});

test('从未见过的旧字幕和其他参与者不能触发当前表现', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(5, { reply_id: 'reply-new' }));
  gate.offer([message(state(1))], 'agent-a');
  assert.equal(gate.read(true, true), null);
  gate.offer(
    [
      message(state(5, { reply_id: 'reply-new' }), {
        from: { identity: 'stranger', isLocal: false },
      }),
    ],
    'agent-a'
  );
  assert.equal(gate.read(true, true), null);
  gate.offer([message(state(5, { reply_id: 'reply-new' }))], 'agent-a');
  assert.equal(gate.read(true, true)?.style, 'happy');
});

test('本地打断关闭原回复，只有观察关闭后新回复才能恢复', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(1));
  gate.offer([message(state(1))], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.interrupt();
  gate.receive(state(2));
  gate.offer([message(state(2))], 'agent-a');
  assert.equal(gate.read(true, true), null);
  gate.receive(state(3, { state: 'closed', style: 'neutral', gesture: 'none' }));
  gate.receive(state(4, { reply_id: 'reply-new' }));
  gate.offer([message(state(4, { reply_id: 'reply-new' }))], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.receive(state(3, { state: 'closed' }));
  assert.ok(gate.read(true, true));
});

test('结束立即清表现，重新扫描历史不能重复触发已经播放的手势', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(1));
  gate.offer([message(state(1))], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.receive(state(2, { state: 'closed' }));
  gate.offer([message(state(1))], 'agent-a');
  assert.equal(gate.read(true, true), null);
});

/** 仅接受当前协议，旧包和非法数值不能把未知动作送入参数控制器。 */
test('未知版本和损坏字段不进入人物协议', () => {
  for (const bad of [
    { ...state(1), v: 1 },
    { ...state(1), gesture: 'execute' },
    { ...state(1), style: 'angry' },
    { ...state(1), revision: -1 },
    { ...state(1), revision: NaN },
    { ...state(1), segment_id: '' },
  ]) {
    assert.equal(parseDelivery(JSON.stringify(bad)), null);
  }
  assert.equal(parseDelivery('{'), null);
});

/** 新风格和动作只扩展枚举，不越过实际可听门或取消栅栏，未知协议仍被原有校验拒绝。 */
test('shy和surprised/shake经过原有音频门控并保留取消栅栏', () => {
  for (const [style, gesture] of [
    ['shy', 'shy'],
    ['surprised', 'shake'],
  ]) {
    const shy = state(1, { style, gesture });
    assert.deepEqual(parseDelivery(JSON.stringify(shy)), shy);
    const gate = new DeliveryGate();
    gate.snapshot(state(0, { state: 'closed' }));
    gate.receive(shy);
    gate.offer([message(shy)], 'agent-a');
    assert.equal(gate.read(false, true), null);
    assert.equal(gate.read(true, false), null);
    assert.equal(gate.read(true, true)?.style, style);
    assert.equal(gate.read(true, true)?.gesture, gesture);
    gate.interrupt();
    const repeated = state(2, { style, gesture });
    gate.receive(repeated);
    gate.offer([message(repeated)], 'agent-a');
    assert.equal(gate.read(true, true), null);
    const next = state(3, { reply_id: 'new-reply', style, gesture });
    gate.receive(next);
    gate.offer([message(next)], 'agent-a');
    assert.equal(gate.read(true, true)?.gesture, gesture);
  }
});

test('相邻句段合并显示，但跨用户发言或不同回复不合并', () => {
  const first = message(state(1));
  const second = message(state(2), { message: '慢慢来。' });
  const user = {
    id: 'user',
    message: '好的',
    timestamp: 2,
    from: { identity: 'user', isLocal: true },
  };
  const groups = groupReplyMessages([
    first,
    second,
    user,
    message(state(3, { reply_id: 'other' })),
  ]);
  assert.equal(groups.length, 3);
  assert.equal(groups[0].id, first.id);
  assert.equal(groups[0].message, '你好。慢慢来。');
  assert.equal(first.message, '你好。');
});

/** 初次音轨和分析器还在准备不等于用户打断，开场许可不能被可听条件提前消费。 */
test('空闲快照后首次开场白等待音频就绪，随后能正常表现', () => {
  const gate = new DeliveryGate();
  const idle = state(0, {
    reply_id: '',
    segment_id: '',
    state: 'closed',
    style: 'neutral',
    gesture: 'none',
  });
  assert.ok(parseDelivery(JSON.stringify(idle)));
  gate.snapshot(idle);
  const greeting = state(1, { gesture: 'wave' });
  gate.receive(greeting);
  gate.offer([message(greeting)], 'agent-a');
  assert.equal(gate.read(false, false), null);
  assert.equal(gate.read(true, false), null);
  assert.equal(gate.read(true, true)?.gesture, 'wave');
});

/** 可靠包仍可能发送失败；更高版本的唯一权威新回复本身就证明旧回复已失去所有权。 */
test('打断后漏收旧closed也可接受新回复，旧回复后续句段仍被隔离', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(1));
  gate.offer([message(state(1))], 'agent-a');
  assert.ok(gate.read(true, true));
  gate.interrupt();
  gate.receive(state(2));
  gate.offer([message(state(2))], 'agent-a');
  assert.equal(gate.read(true, true), null);
  const next = state(4, { reply_id: 'reply-next', style: 'gentle', gesture: 'none' });
  gate.receive(next);
  gate.offer([message(next)], 'agent-a');
  assert.equal(gate.read(true, true)?.style, 'gentle');
  gate.receive(state(3, { state: 'closed', style: 'neutral', gesture: 'none' }));
  gate.offer([message(state(1)), message(next)], 'agent-a');
  assert.equal(gate.read(true, true)?.id, 'job-a:4:segment-4');
});

/** 连接回调必须隔离，但断线前已取消的整个回复不能因恢复快照成为新的表演许可。 */
test('跨连接控制器保留取消回复，新段不复活且新回复正常恢复', () => {
  const first = new DeliveryGate();
  first.snapshot(state(1));
  first.offer([message(state(1))], 'agent-a');
  assert.ok(first.read(true, true));
  first.interrupt();
  const disconnected = new DeliveryGate(first);
  const restored = new DeliveryGate(disconnected);
  restored.receive(state(3));
  restored.snapshot(state(2));
  restored.offer([message(state(3))], 'agent-a');
  assert.equal(restored.read(true, true), null);
  const next = state(4, { reply_id: 'reply-next' });
  restored.receive(next);
  restored.offer([message(next)], 'agent-a');
  assert.ok(restored.read(true, true));
  first.interrupt();
  assert.ok(restored.read(true, true), '旧代次回收不能取消新控制器');
});

/** 恢复时的旧快照不能回退序号，真正新进程则有独立的序号空间和表达许可。 */
test('恢复拒绝迟到低序号，Agent实例更替允许重新从首段开始', () => {
  const first = new DeliveryGate();
  first.snapshot(state(5));
  first.offer([message(state(5))], 'agent-a');
  assert.ok(first.read(true, true));
  const restored = new DeliveryGate(first);
  restored.snapshot(state(1, { reply_id: 'old-reply' }));
  restored.receive(state(4, { reply_id: 'old-reply' }));
  restored.offer([message(state(4, { reply_id: 'old-reply' }))], 'agent-a');
  assert.equal(restored.read(true, true), null);
  const restarted = new DeliveryGate(restored);
  const nextInstance = state(1, { instance: 'job-b' });
  restarted.snapshot(nextInstance);
  restarted.offer([message(nextInstance)], 'agent-a');
  assert.equal(restarted.read(true, true)?.id, 'job-b:1:segment-1');
});

/** 回复身份独立于段与序号，实例也参与二元编码，防止冒号拼接碰撞把新实例误当旧回复。 */
test('replyKey跨句段与关闭间隔稳定，换回复或实例才改变', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(1, { instance: 'job:a', reply_id: 'reply' }));
  let first;
  for (const revision of [1, 2, 4]) {
    const current = state(revision, { instance: 'job:a', reply_id: 'reply' });
    if (revision > 1) gate.receive(current);
    gate.offer([message(current)], 'agent-a');
    const delivery = gate.read(true, true);
    assert.equal(delivery.replyKey, JSON.stringify(['job:a', 'reply']));
    if (first) assert.notEqual(delivery.id, first.id);
    first ??= delivery;
    if (revision === 2) {
      gate.receive(state(3, { instance: 'job:a', reply_id: 'reply', state: 'closed' }));
      assert.equal(gate.read(true, true), null);
    }
  }
  const next = state(5, { instance: 'job:a', reply_id: 'next' });
  gate.receive(next);
  gate.offer([message(next)], 'agent-a');
  assert.notEqual(gate.read(true, true).replyKey, first.replyKey);
  const restarted = new DeliveryGate();
  const otherInstance = state(1, { instance: 'job', reply_id: 'a:reply' });
  restarted.snapshot(otherInstance);
  restarted.offer([message(otherInstance)], 'agent-a');
  assert.notEqual(restarted.read(true, true).replyKey, first.replyKey);
});

/** 真正失效发生在无 current 时也须保留；先合并较新订阅再取消，不能仅取消较旧快照。 */
test('同步前能力失效取消合并后的整回复，之后新回复正常', () => {
  const gate = new DeliveryGate();
  gate.invalidatePresentation();
  gate.receive(state(3, { reply_id: 'blocked' }));
  gate.interrupt();
  gate.snapshot(state(1));
  gate.offer([message(state(3, { reply_id: 'blocked' }))], 'agent-a');
  assert.equal(gate.diagnostics.blockedReply, 'blocked');
  assert.equal(gate.read(true, true), null);
  gate.receive(state(4, { reply_id: 'blocked' }));
  gate.offer([message(state(4, { reply_id: 'blocked' }))], 'agent-a');
  assert.equal(gate.read(true, true), null);
  gate.receive(state(5, { reply_id: 'new' }));
  gate.offer([message(state(5, { reply_id: 'new' }))], 'agent-a');
  assert.ok(gate.read(true, true));
});

/** 空闲权威快照没有可取消的回复，失效标记消费后不能误伤未来第一次真实发言。 */
test('失效后的空闲快照只建立基线，不永久锁住下次回复', () => {
  const gate = new DeliveryGate();
  gate.invalidatePresentation();
  gate.snapshot(state(0, { state: 'closed', style: 'neutral', gesture: 'none' }));
  gate.receive(state(1));
  gate.offer([message(state(1))], 'agent-a');
  assert.ok(gate.read(true, true));
});

/** 同步前换连接仍要保留真实失效，但普通初始化 interrupt 不能等同于失去播放能力。 */
test('能力失效跨同步代次保留，普通初始化仍允许开场白', () => {
  const original = new DeliveryGate();
  original.invalidatePresentation();
  const replacement = new DeliveryGate(new DeliveryGate(original));
  replacement.snapshot(state(1));
  replacement.offer([message(state(1))], 'agent-a');
  assert.equal(replacement.read(true, true), null);
  const initial = new DeliveryGate();
  initial.interrupt();
  initial.snapshot(state(1));
  initial.offer([message(state(1))], 'agent-a');
  assert.ok(initial.read(true, true));
});

/** 可变视觉版本不属于字幕身份，Jev 迟到增强可直接应用而不等待另一份文本。 */
test('较新视觉状态接受旧版同片段字幕头并保持稳定动作身份', () => {
  const gate = new DeliveryGate();
  const baseline = state(1);
  gate.snapshot(baseline);
  gate.offer([message(baseline)], 'agent-a');
  const first = gate.read(true, true);
  const enhanced = { ...baseline, revision: 2, style: 'gentle', energy: 0.2 };
  gate.receive(enhanced);
  gate.offer([message(baseline)], 'agent-a');
  const updated = gate.read(true, false);
  assert.equal(updated.style, 'gentle');
  assert.equal(updated.energy, 0.2);
  assert.equal(updated.gestureId, first.gestureId);
  assert.equal(updated.segmentKey, first.segmentKey);
  assert.notEqual(updated.id, first.id);
});

/** 已播放许可仅限当前片段，新片段即使同回复也仍需匹配字幕和真实出声。 */
test('视觉修订复用当前许可而新片段不能利用上一段静音保持', () => {
  const gate = new DeliveryGate();
  const first = state(1);
  gate.snapshot(first);
  gate.offer([message(first)], 'agent-a');
  assert.ok(gate.read(true, true));
  const next = state(2);
  gate.receive(next);
  gate.offer([message(first)], 'agent-a');
  assert.equal(gate.read(true, true), null);
  gate.offer([message(next)], 'agent-a');
  assert.equal(gate.read(true, false), null);
  assert.ok(gate.read(true, true));
});

/** 倾听和思考是当前用户轮次的微表情，不依赖字幕、音轨或浏览器播放许可。 */
test('倾听和思考无需声音但仍受连接可见性及当前轮次控制', () => {
  for (const phase of ['listening', 'thinking']) {
    const gate = new DeliveryGate();
    const current = state(1, {
      phase,
      reply_id: '',
      segment_id: '',
      gesture: 'none',
      gesture_id: '',
      style: 'concerned',
    });
    gate.snapshot(current);
    assert.equal(gate.read(false, false).phase, phase);
    assert.equal(gate.read(false, false).style, 'concerned');
    gate.interruptSpeaking();
    gate.invalidatePlayback();
    assert.equal(gate.read(false, false).phase, phase);
    assert.equal(gate.read(false, false, false), null);
    gate.interrupt();
    gate.receive({ ...current, revision: 2 });
    assert.equal(gate.read(false, false), null);
    gate.receive({ ...current, revision: 3, turn_id: 'new-turn' });
    assert.equal(gate.read(false, false).turnId, 'new-turn');
  }
});

/** 音频尚未准备只阻止说话；真正后台失效必须阻止恢复快照中的倾听轮次。 */
test('同步前播放受阻允许倾听，后台失效跨连接阻止旧倾听快照', () => {
  const listening = state(1, {
    phase: 'listening',
    reply_id: '',
    segment_id: '',
    gesture: 'none',
    gesture_id: '',
  });
  const waitingForAudio = new DeliveryGate();
  waitingForAudio.invalidatePlayback();
  waitingForAudio.snapshot(listening);
  assert.ok(waitingForAudio.read(false, false));
  const hidden = new DeliveryGate();
  hidden.invalidatePresentation();
  const restored = new DeliveryGate(hidden);
  restored.snapshot(listening);
  assert.equal(restored.read(false, false), null);
  const fresh = new DeliveryGate(restored);
  fresh.snapshot({ ...listening, instance: 'job-new' });
  assert.ok(fresh.read(false, false));
});

/** 被打断过的回复不能因另一回复开始而解除封存，服务端乱序的更高版本仍须拒绝。 */
test('新回复开始后迟到旧回复仍无法复活，两个gate互不影响', () => {
  const gate = new DeliveryGate();
  const other = new DeliveryGate();
  for (const candidate of [gate, other]) {
    candidate.snapshot(state(1));
    candidate.offer([message(state(1))], 'agent-a');
    assert.ok(candidate.read(true, true));
  }
  gate.interruptSpeaking();
  const next = state(2, { reply_id: 'reply-new' });
  gate.receive(next);
  gate.offer([message(next)], 'agent-a');
  assert.ok(gate.read(true, true));
  const stale = state(3);
  gate.receive(stale);
  gate.offer([message(stale)], 'agent-a');
  assert.equal(gate.read(true, true), null);
  assert.ok(other.read(true, true));
});

/** v3 每个新增字段都经过校验，未知版本和越界强度不能进入动画数值通道。 */
test('v3必填字段及受控动作规则严格校验', () => {
  for (const field of [
    'phase',
    'turn_id',
    'gesture_id',
    'gesture_source',
    'command_feedback',
    'energy',
  ]) {
    const missing = state(1);
    delete missing[field];
    assert.equal(parseDelivery(JSON.stringify(missing)), null);
  }
  for (const overrides of [
    { v: 1 },
    { phase: 'dancing' },
    { turn_id: '' },
    { energy: -0.1 },
    { energy: 1.1 },
    { energy: null },
    { gesture_id: '' },
    { phase: 'thinking' },
  ])
    assert.equal(parseDelivery(JSON.stringify(state(1, overrides))), null);
  const pureListening = state(1, {
    phase: 'listening',
    reply_id: '',
    segment_id: '',
    gesture: 'none',
    gesture_id: '',
  });
  assert.deepEqual(parseDelivery(JSON.stringify(pureListening)), pureListening);
});

/** 指令执行权独立于语音阶段；同一身份在结束回复后仍然可执行而不借用声音。 */
test('指令跨thinking、speaking与idle保持身份，无声和播放受阻仍有效', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(0, { state: 'closed' }));
  for (const [index, phase] of ['thinking', 'speaking', 'idle'].entries()) {
    const current = state(index + 1, {
      phase,
      reply_id: '',
      segment_id: '',
      turn_id: 'command-turn',
      gesture: 'wave',
      gesture_source: 'command',
      gesture_id: 'command-a',
    });
    assert.ok(parseDelivery(JSON.stringify(current)));
    gate.receive(current);
    gate.invalidatePlayback();
    assert.equal(gate.read(false, false)?.gestureId, 'command-a');
    assert.equal(gate.audibleSpeechAuthorized, false);
  }
  gate.interrupt();
  gate.receive(
    state(4, { gesture_source: 'command', gesture_id: 'command-a', turn_id: 'command-turn' })
  );
  assert.equal(gate.read(false, false), null);
});

/** 快照恢复神态但不恢复执行机会，更高修订重复同一动作身份仍不能补演。 */
test('快照中的倾听、说话及指令动作全部只恢复神态', () => {
  for (const [phase, source] of [
    ['listening', 'dialogue'],
    ['speaking', 'dialogue'],
    ['idle', 'command'],
  ]) {
    const gate = new DeliveryGate();
    const current = state(1, { phase, gesture_source: source, gesture: 'tilt' });
    gate.snapshot(current);
    gate.offer([message(current)], 'agent-a');
    assert.equal(gate.read(true, true)?.gesture, 'none');
    assert.equal(gate.read(true, true)?.style, 'happy');
    gate.receive({ ...current, revision: 2 });
    gate.offer([message(current)], 'agent-a');
    assert.equal(gate.read(true, true)?.gesture, 'none');
    gate.receive({ ...current, revision: 3, turn_id: 'new-turn', gesture_id: 'new-gesture' });
    gate.offer([message({ ...current, turn_id: 'new-turn' })], 'agent-a');
    assert.equal(gate.read(true, true)?.gesture, 'tilt');
  }
});

/** 真实指令可以独立于字幕执行，嘴部授权仍要求同片段字幕和实际PCM。 */
test('无字幕指令不会绕过口型播放门，取消说话只关闭嘴部', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(0, { state: 'closed' }));
  const current = state(1, { gesture_source: 'command' });
  gate.receive(current);
  assert.equal(gate.read(true, true)?.gesture, 'nod');
  assert.equal(gate.audibleSpeechAuthorized, false);
  gate.offer([message(current)], 'agent-a');
  gate.read(true, true);
  assert.equal(gate.audibleSpeechAuthorized, true);
  gate.interruptSpeaking();
  assert.equal(gate.read(true, true)?.gesture, 'nod');
  assert.equal(gate.audibleSpeechAuthorized, false);
});

/** 明确执行终态不可重复或回退；未知和快照动作不能伪造确认。 */
test('指令回执幂等并保留最近实时指令供主动重试', () => {
  const gate = new DeliveryGate();
  const received = [];
  gate.setAcknowledgmentSink((ack) => received.push(ack));
  gate.snapshot(state(0, { state: 'closed' }));
  const current = state(1, { gesture: 'shake', gesture_source: 'command' });
  gate.receive(current);
  const ack = {
    instance: current.instance,
    turn_id: current.turn_id,
    gesture_id: current.gesture_id,
  };
  gate.acknowledge({ ...ack, status: 'completed' });
  gate.acknowledge({ ...ack, gesture_id: 'unknown', status: 'started' });
  assert.equal(received.length, 0);
  for (const status of ['started', 'started', 'completed', 'completed', 'canceled'])
    gate.acknowledge({ ...ack, status });
  assert.deepEqual(
    received.map((ack) => ack.status),
    ['started', 'completed']
  );
  gate.receive(state(2, { gesture: 'none', command_feedback: 'retry' }));
  assert.deepEqual(gate.getFeedback(), { feedback: 'retry', gesture: 'shake' });
});

/** 未知判断没有动作授权，保持 retry 也不能借用上一轮的指令；新轮已知动作仍可恢复。 */
test('本轮已知指令可以重试，跨轮未知反馈不继承上一动作', () => {
  const gate = new DeliveryGate();
  gate.snapshot(state(0, { state: 'closed' }));
  gate.receive(state(1, { gesture: 'wave', gesture_source: 'command', turn_id: 'previous-turn' }));
  gate.receive(state(2, { gesture: 'none', command_feedback: 'retry', turn_id: 'previous-turn' }));
  assert.deepEqual(gate.getFeedback(), { feedback: 'retry', gesture: 'wave' });
  gate.receive(
    state(3, { gesture: 'none', command_feedback: 'retry', turn_id: 'unknown-current-turn' })
  );
  assert.deepEqual(gate.getFeedback(), { feedback: 'retry', gesture: null });
  gate.receive(
    state(4, { gesture: 'nod', gesture_source: 'command', turn_id: 'unknown-current-turn' })
  );
  gate.receive(
    state(5, { gesture: 'none', command_feedback: 'retry', turn_id: 'unknown-current-turn' })
  );
  assert.deepEqual(gate.getFeedback(), { feedback: 'retry', gesture: 'nod' });
});

/** 连接换代可保留本轮已知重试，但同名轮次属于另一个 Agent 时不能继承旧指令。 */
test('重连保留相同实例轮次的重试，跨实例同名轮次不串动作', () => {
  const first = new DeliveryGate();
  first.snapshot(state(0, { state: 'closed' }));
  first.receive(state(1, { gesture: 'shake', gesture_source: 'command' }));
  const sameOwner = new DeliveryGate(first);
  sameOwner.snapshot(state(2, { gesture: 'none', command_feedback: 'retry' }));
  assert.deepEqual(sameOwner.getFeedback(), { feedback: 'retry', gesture: 'shake' });
  const nextOwner = new DeliveryGate(first);
  nextOwner.snapshot(state(2, { instance: 'job-b', gesture: 'none', command_feedback: 'retry' }));
  assert.deepEqual(nextOwner.getFeedback(), { feedback: 'retry', gesture: null });
});
