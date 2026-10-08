import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const source = await readFile(new URL('../lib/conversation-end.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const requestId = '123e4567-e89b-42d3-a456-426614174000';
const events = { DataReceived: 'data', ConnectionStateChanged: 'state' };
const agent = { identity: 'agent', sid: 'PA_agent', kind: 4 };

/** 真实模块仅替换可靠传输和时间源，确认、来源校验及释放路径仍执行生产源码。 */
function harness(publish) {
  const timers = new Set();
  const sent = [];
  const room = new EventEmitter();
  room.state = 'connected';
  room.localParticipant = {
    /** 在发送期间同步回包，验证监听必须早于请求，不能漏掉低延迟确认。 */
    async publishData(data, options) {
      sent.push({ packet: JSON.parse(new TextDecoder().decode(data)), options });
      if (publish) return publish(room);
    },
  };
  const exports = {};
  const context = vm.createContext({
    exports,
    /** 测试禁止加载真实 SDK 或网络，仅提供其公开枚举。 */
    require(name) {
      assert.equal(name, 'livekit-client');
      return {
        ConnectionState: { Connected: 'connected' },
        RoomEvent: events,
        DataPacket_Kind: { RELIABLE: 0 },
        ParticipantKind: { AGENT: 4 },
      };
    },
    crypto: { randomUUID: () => requestId },
    TextEncoder,
    TextDecoder,
    /** 只模拟一秒边界，不让发送卡住测试或遗留真实计时器。 */
    setTimeout(callback, delay) {
      assert.equal(delay, 1_000);
      const timer = { callback };
      timers.add(timer);
      return timer;
    },
    /** 确认或断线都应显式移除等待句柄。 */
    clearTimeout(timer) {
      timers.delete(timer);
    },
  });
  new vm.Script(code).runInContext(context);
  return { room, timers, sent, end: exports.acknowledgeConversationEnd };
}

/** 只有协议、请求和真实 SDK 来源全部匹配才认为 Agent 接受了本次结束。 */
function acknowledge(room, body = {}, sender = agent, kind = 0, topic = 'xiaoya.delivery') {
  room.emit(
    events.DataReceived,
    new TextEncoder().encode(
      JSON.stringify({
        v: 1,
        type: 'user_end_ack',
        request_id: requestId,
        agent_sid: agent.sid,
        ...body,
      })
    ),
    sender,
    kind,
    topic
  );
}

/** 可靠入队不是送达，仍等待真实确认；快速同步确认也不能丢失。 */
test('先监听再可靠定向发送，正确确认后移除全部句柄', async () => {
  const probe = harness((room) => acknowledge(room));
  assert.equal(await probe.end(probe.room, agent), true);
  assert.equal(probe.sent[0].packet.type, 'user_end');
  assert.equal(probe.sent[0].packet.target_agent_sid, agent.sid);
  assert.deepEqual(JSON.parse(JSON.stringify(probe.sent[0].options)), {
    reliable: true,
    topic: 'xiaoya.delivery',
    destinationIdentities: ['agent'],
  });
  assert.equal(probe.timers.size, 0);
  assert.equal(probe.room.listenerCount(events.DataReceived), 0);
  assert.equal(probe.room.listenerCount(events.ConnectionStateChanged), 0);
});

/** 伪造来源、旧请求和损坏包只能被忽略，不能借助手势主题提前确认挂断。 */
test('错误来源、SID、版本、请求、主题、传输种类和超包均不确认', async () => {
  const probe = harness();
  const pending = probe.end(probe.room, agent);
  acknowledge(probe.room, {}, { identity: 'stranger', sid: agent.sid });
  acknowledge(probe.room, {}, { identity: agent.identity, sid: 'PA_old' });
  acknowledge(probe.room, {}, { ...agent, kind: 0 });
  acknowledge(probe.room, { v: 2 });
  acknowledge(probe.room, { request_id: 'old' });
  acknowledge(probe.room, { agent_sid: 'PA_old' });
  acknowledge(probe.room, {}, agent, 1);
  acknowledge(probe.room, {}, agent, 0, 'other');
  acknowledge(probe.room, { extra: 'x'.repeat(512) });
  acknowledge(probe.room, { extra: 'unexpected' });
  probe.room.emit(events.DataReceived, new TextEncoder().encode('{'), agent, 0, 'xiaoya.delivery');
  assert.equal(probe.timers.size, 1);
  acknowledge(probe.room);
  assert.equal(await pending, true);
});

/** 发送永久挂起也只能占用一秒，结束后的迟到确认与计时器均无副作用。 */
test('发送卡住时一秒兜底，迟到确认不会遗留监听', async () => {
  const probe = harness(() => new Promise(() => undefined));
  const pending = probe.end(probe.room, agent);
  const timer = [...probe.timers][0];
  timer.callback();
  assert.equal(await pending, false);
  acknowledge(probe.room);
  timer.callback();
  assert.equal(probe.room.listenerCount(events.DataReceived), 0);
  assert.equal(probe.timers.size, 0);
});

/** 连接失去许可即退出等待，不把可靠队列在新连接重放当成本轮确认。 */
test('断线和发送失败立即降级，缺少 Agent 或未连接不发包', async () => {
  const disconnected = harness();
  const pending = disconnected.end(disconnected.room, agent);
  disconnected.room.emit(events.ConnectionStateChanged, 'reconnecting');
  assert.equal(await pending, false);
  assert.equal(disconnected.timers.size, 0);
  const rejected = harness(async () => {
    throw new Error('模拟传输失败');
  });
  assert.equal(await rejected.end(rejected.room, agent), false);
  assert.equal(rejected.timers.size, 0);
  assert.equal(await rejected.end(rejected.room), false);
  assert.equal(await rejected.end(rejected.room, { ...agent, kind: 0 }), false);
  rejected.room.state = 'disconnected';
  assert.equal(await rejected.end(rejected.room, agent), false);
  assert.equal(rejected.sent.length, 1);
});
