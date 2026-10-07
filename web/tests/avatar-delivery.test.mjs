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
