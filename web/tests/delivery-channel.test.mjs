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

/** 回执由真实 gate 的执行事件入口生成；重连或两端 SID 换代后禁止向旧 Agent 发送。 */
test('渲染回执发送一次且旧连接、重复终态与未知身份不发送', async () => {
  const view = await channel();
  assert.equal(await view.controller.ready, true);
  const current = delivery(1, {
    state: 'active',
    phase: 'idle',
    turn_id: 'command-turn',
    gesture: 'wave',
    gesture_source: 'command',
    gesture_id: 'command-a',
  });
  publish(view, current);
  const ack = {
    instance: current.instance,
    turn_id: current.turn_id,
    gesture_id: current.gesture_id,
  };
  for (const status of ['started', 'started', 'completed', 'completed', 'canceled'])
    view.controller.gate.acknowledge({ ...ack, status });
  const sent = view.room.rpcCalls.filter((request) => request.method === 'xiaoya.ackPresentation');
  assert.deepEqual(
    sent.map((request) => JSON.parse(request.payload).status),
    ['started', 'completed']
  );
  assert.ok(
    sent.every(
      (request) =>
        request.destinationIdentity === view.agent.identity && request.responseTimeout === 1000
    )
  );
  publish(view, { ...current, revision: 2, gesture_id: 'command-b' });
  view.room.localParticipant.sid = 'PA_changed';
  view.controller.gate.acknowledge({ ...ack, gesture_id: 'command-b', status: 'started' });
  assert.equal(
    view.room.rpcCalls.filter((request) => request.method === 'xiaoya.ackPresentation').length,
    2
  );
});

/** 同一个RPC原样传递renderer的未发力事实，传输层不能以字段缺失自行补false或伪称采纳。 */
test('当前自然动作的stroke_started=false按原RPC与所有者发送', async () => {
  const view = await channel();
  assert.equal(await view.controller.ready, true);
  const current = delivery(1, {
    state: 'active',
    phase: 'listening',
    turn_id: 'listen-turn',
    gesture: 'nod',
    gesture_source: 'dialogue',
    gesture_id: 'listen-nod',
  });
  publish(view, current);
  const ack = {
    instance: current.instance,
    turn_id: current.turn_id,
    gesture_id: current.gesture_id,
    status: 'canceled',
    stroke_started: false,
  };
  view.controller.gate.acknowledge(ack);
  const sent = view.room.rpcCalls.filter((request) => request.method === 'xiaoya.ackPresentation');
  assert.equal(sent.length, 1);
  assert.deepEqual(JSON.parse(sent[0].payload), ack);
  assert.equal(sent[0].destinationIdentity, view.agent.identity);
  assert.equal(sent[0].responseTimeout, 1000);
  view.controller.close();
});

/** 本地 VU 的短暂停顿不等于后端的新轮次，继续说话不能封存仍在倾听的当前轮。 */
test('VAD取消旧指令但持续讲话和短暂停顿保留已确认的倾听轮次', async () => {
  const view = await channel();
  await view.controller.ready;
  const command = delivery(1, {
    state: 'active',
    phase: 'thinking',
    turn_id: 'old-turn',
    gesture: 'wave',
    gesture_source: 'command',
  });
  publish(view, command);
  view.room.localParticipant.isSpeaking = true;
  view.room.emit(events.ActiveSpeakersChanged);
  assert.equal(view.controller.gate.read(false, false), null);
  const listening = delivery(2, {
    state: 'active',
    phase: 'listening',
    turn_id: 'listening-turn',
    gesture: 'tilt',
    gesture_source: 'dialogue',
  });
  publish(view, listening);
  view.room.emit(events.ActiveSpeakersChanged);
  assert.equal(view.controller.gate.read(false, false)?.gesture, 'tilt');
  view.room.localParticipant.isSpeaking = false;
  view.room.emit(events.ActiveSpeakersChanged);
  view.room.localParticipant.isSpeaking = true;
  view.room.emit(events.ActiveSpeakersChanged);
  assert.equal(view.controller.gate.read(false, false)?.gesture, 'tilt');
});

/** 后端 VAD 可先于 SDK 扬声器事件；按实际逆序投递，避免只验证理想事件顺序。 */
test('后端倾听先到再收到首次本地起声时仍展示语义并允许同轮回复', async () => {
  const view = await channel();
  await view.controller.ready;
  const listening = delivery(1, {
    state: 'active',
    phase: 'listening',
    turn_id: 'new-listening-turn',
    reply_id: '',
    segment_id: '',
    style: 'happy',
    gesture: 'nod',
    gesture_source: 'dialogue',
  });
  publish(view, listening);
  view.room.localParticipant.isSpeaking = true;
  view.room.emit(events.ActiveSpeakersChanged);
  const reaction = view.controller.gate.read(false, false);
  assert.equal(reaction?.turnId, listening.turn_id);
  assert.equal(reaction?.style, 'happy');
  assert.equal(reaction?.gesture, 'nod');
  const reply = delivery(2, {
    state: 'active',
    phase: 'speaking',
    turn_id: listening.turn_id,
    reply_id: 'same-turn-reply',
    segment_id: 'same-turn-segment',
    gesture: 'none',
  });
  publish(view, reply);
  assert.equal(view.controller.gate.read(true, true)?.turnId, listening.turn_id);
  assert.equal(view.controller.gate.audibleSpeechAuthorized, true);
});

/** 指令即使共享 listening 语音阶段也必须服从新起声取消，来源比阶段更能界定执行许可。 */
test('本地起声仍取消listening阶段的独立指令且只报告一次取消', async () => {
  const view = await channel();
  await view.controller.ready;
  const command = delivery(1, {
    state: 'active',
    phase: 'listening',
    turn_id: 'command-turn',
    gesture: 'wave',
    gesture_source: 'command',
  });
  publish(view, command);
  view.room.localParticipant.isSpeaking = true;
  view.room.emit(events.ActiveSpeakersChanged);
  view.room.emit(events.ActiveSpeakersChanged);
  assert.equal(view.controller.gate.read(false, false), null);
  const receipts = view.room.rpcCalls.filter(
    (request) => request.method === 'xiaoya.ackPresentation'
  );
  assert.equal(receipts.length, 1);
  assert.equal(JSON.parse(receipts[0].payload).status, 'canceled');
});
