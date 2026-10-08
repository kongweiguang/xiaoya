import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { conversationPorts, delivery, events } from './helpers/conversation-ports.mjs';
import { deferred, production, settle } from './helpers/dom.mjs';

const channels = new Set();
/** 回收通道的监听器与计时器，异步重试不得跨测试影响后续权威快照。 */
afterEach(() => {
  for (const channel of channels) channel.close();
  channels.clear();
});

/** 端口提供真实成员表和可靠数据事件，就绪与动画使用同一个生产通道。 */
async function channel(options = {}) {
  const environment = conversationPorts();
  const room = new environment.ports['livekit-client'].Room({});
  await room.start();
  Object.assign(room, options);
  const { DeliveryChannel } = await production('lib/delivery-channel.ts', environment.ports);
  let invalidations = 0;
  const controller = new DeliveryChannel(room, environment.agent, new DeliveryGate(), () => {
    invalidations++;
  });
  channels.add(controller);
  return { ...environment, room, controller, invalidations: () => invalidations };
}

/** 字幕锚点携带当前协议，不能由测试直接写入 DeliveryGate 私有状态。 */
function message(state) {
  return {
    id: state.segment_id,
    message: '你好。',
    from: { identity: 'agent', isLocal: false },
    attributes: { 'xiaoya.delivery': JSON.stringify(state), 'lk.segment_id': state.segment_id },
  };
}

/** 包来源使用 SDK 参与者对象，允许测试改变可靠性与 SID 而不复制生产鉴权。 */
function publish(view, state, participant = view.agent, kind = 0, topic = 'xiaoya.delivery') {
  view.controller.offer([message(state)]);
  view.room.emit(
    events.DataReceived,
    new TextEncoder().encode(JSON.stringify(state)),
    participant,
    kind,
    topic
  );
}

/** 订阅在快照前建立，RPC 期间更高修订包最终合并而不倒退。 */
test('一个通道同时证明就绪并同步表现，快照与订阅正确合并', async () => {
  const pending = deferred();
  const view = await channel({ rpcGate: pending });
  assert.equal(view.room.rpcCalls.length, 1);
  assert.equal(view.room.rpcCalls[0].subscribed, true);
  const active = delivery(2, {
    state: 'active',
    reply_id: 'reply-a',
    segment_id: 'seg-a',
    style: 'happy',
    gesture: 'wave',
  });
  publish(view, active);
  await settle(() => pending.resolve(JSON.stringify(delivery(1))));
  assert.equal(await view.controller.ready, true);
  assert.equal(view.controller.gate.diagnostics.state.revision, 2);
  assert.equal(view.controller.gate.read(true, true).gesture, 'wave');
});

/** SID 变化不会被旧成功响应授权，哪怕 SDK Room 和参与者对象引用没有改变。 */
test('RPC 等待中两端任意 SID 变化均拒绝旧确认', async () => {
  for (const side of ['local', 'agent']) {
    const pending = deferred();
    const view = await channel({ rpcGate: pending });
    if (side === 'local') view.room.localParticipant.sid = 'PA_new_local';
    else view.agent.sid = 'PA_new_agent';
    await settle(() => pending.resolve(JSON.stringify(delivery())));
    assert.equal(await view.controller.ready, false);
    assert.equal(view.controller.gate.diagnostics.synchronized, false);
    view.controller.close();
  }
});

/** 取消立即结束等待并解除订阅，SDK 的不可取消 RPC 迟到成功不能重新激活动画。 */
test('取消共享等待后迟到快照不能写入表现控制器', async () => {
  const pending = deferred();
  const view = await channel({ rpcGate: pending });
  view.controller.close();
  assert.equal(await view.controller.ready, false);
  assert.equal(view.room.listenerCount(events.DataReceived), 0);
  await settle(() => pending.resolve(JSON.stringify(delivery())));
  assert.equal(view.controller.gate.diagnostics.synchronized, false);
});

/** presence 尚未广播时仍要逐次真实鉴权；第五次成功位于四秒间隔内，不降级成默认许可。 */
test('presence 暂时拒绝共享重试五次，超时后不再请求', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const view = await channel({
    rpcResponses: [
      new Error('pending'),
      new Error('pending'),
      new Error('pending'),
      new Error('pending'),
      JSON.stringify(delivery()),
    ],
  });
  await settle();
  for (const delay of [400, 800, 1200, 1600]) {
    t.mock.timers.tick(delay);
    await settle();
  }
  assert.equal(await view.controller.ready, true);
  assert.equal(view.room.rpcCalls.length, 5);
  assert.ok(view.room.rpcCalls.every((request) => request.subscribed));
  view.controller.close();
  t.mock.timers.tick(10_000);
  assert.equal(view.room.rpcCalls.length, 5);
  t.mock.timers.reset();
});

/** 挂住的 SDK 请求不能阻止五秒到期或取消，计时器之后的旧返回仍然无权写入。 */
test('共享快照等待有五秒截止点且非法快照不会放行', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const pending = deferred();
  const view = await channel({ rpcGate: pending });
  t.mock.timers.tick(5000);
  assert.equal(await view.controller.ready, false);
  assert.equal(view.room.listenerCount(events.DataReceived), 0);
  await settle(() => pending.resolve(JSON.stringify(delivery())));
  assert.equal(view.controller.gate.diagnostics.synchronized, false);
  t.mock.timers.reset();
  const invalid = await channel({ rpcResponses: ['{"v":1}'] });
  assert.equal(await invalid.controller.ready, false);
});

/** SDK 的可靠性、身份、SID 和 Agent 类型缺一不可，正文中的自报身份不能绕过它。 */
test('只有当前 Agent 的可靠包改变表现', async () => {
  const view = await channel();
  assert.equal(await view.controller.ready, true);
  const active = delivery(1, {
    state: 'active',
    reply_id: 'reply-a',
    segment_id: 'seg-a',
    style: 'happy',
    gesture: 'wave',
  });
  for (const [participant, kind, topic] of [
    [undefined, 0, 'xiaoya.delivery'],
    [{ ...view.agent, kind: 0 }, 0, 'xiaoya.delivery'],
    [{ ...view.agent, identity: 'other' }, 0, 'xiaoya.delivery'],
    [{ ...view.agent, sid: 'PA_old' }, 0, 'xiaoya.delivery'],
    [view.agent, 1, 'xiaoya.delivery'],
    [view.agent, 0, 'other'],
  ]) {
    view.room.emit(
      events.DataReceived,
      new TextEncoder().encode(JSON.stringify(active)),
      participant,
      kind,
      topic
    );
    assert.equal(view.controller.gate.diagnostics.state.revision, 0);
  }
  publish(view, active);
  assert.equal(view.controller.gate.read(true, true).gesture, 'wave');
});

/** 重连和 Agent 离场都撤销控制订阅，但无关参与者不影响当前许可。 */
test('信令丢失与真实 Agent 离场立即取消，其他参与者离场忽略', async () => {
  const view = await channel();
  assert.equal(await view.controller.ready, true);
  view.room.emit(events.ParticipantDisconnected, { identity: 'other', sid: 'PA_other' });
  assert.equal(view.invalidations(), 0);
  view.room.setState('signal-reconnecting');
  assert.equal(view.invalidations(), 1);
  assert.equal(view.room.listenerCount(events.DataReceived), 0);
});
