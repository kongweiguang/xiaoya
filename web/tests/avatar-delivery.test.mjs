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
    v: 1,
    instance: 'job-a',
    revision,
    reply_id: 'reply-a',
    segment_id: `segment-${revision}`,
    state: 'active',
    style: 'happy',
    gesture: 'nod',
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

test('未知版本和损坏字段不进入人物协议', () => {
  for (const bad of [
    { ...state(1), v: 2 },
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
    gate.snapshot(shy);
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
