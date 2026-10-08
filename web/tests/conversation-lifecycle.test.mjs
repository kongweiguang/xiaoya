import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { LocalTrack, conversationPorts, events } from './helpers/conversation-ports.mjs';
import {
  React,
  cleanup,
  deferred,
  fireEvent,
  production,
  render,
  settle,
  waitFor,
} from './helpers/dom.mjs';

afterEach(cleanup);

/** 视觉舞台有独立资源测试，此处仅替换它的模型端口而保留真实页面、控件及会话宿主。 */
async function app(options = {}) {
  const environment = conversationPorts(options);
  const { App } = await production('components/app/app.tsx', {
    ...environment.ports,
    '@/components/app/avatar-stage': {
      AvatarStage: ({ status }) =>
        React.createElement(
          'section',
          { 'aria-label': '小芽数字人' },
          React.createElement('p', { role: 'status' }, status)
        ),
    },
  });
  const view = render(
    React.createElement(
      React.StrictMode,
      null,
      React.createElement(App, { tokenEndpoint: '/api/token' })
    )
  );
  return { ...environment, ...view };
}

/** 欢迎页零媒体分配，开始才分配一个 Room／时钟，双击不会另建连接或音频出口。 */
test('点击开始才创建资源，SDK 上下文使用当前房间且保持双连接配置', async () => {
  const view = await app();
  assert.equal(view.rooms.length, 0);
  assert.equal(view.contexts.length, 0);
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  fireEvent.click(view.getByRole('button', { name: /正在连接/ }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  assert.equal(view.rooms.length, 1);
  assert.equal(view.contexts.length, 1);
  assert.equal(view.rooms[0].config.singlePeerConnection, false);
  assert.equal(view.rooms[0].config.webAudioMix.audioContext, view.contexts[0]);
  assert.equal(view.calls.microphones.length, 0);
  assert.equal(view.rooms[0].starts, 1);
  assert.equal(view.calls.renderers, 1);
  assert.ok(
    view.calls.sessionOptions.every(
      (config) => config.room === view.rooms[0] && !('agentName' in config)
    )
  );
  assert.equal(view.rooms[0].rpcCalls.length, 1);
  assert.ok(view.rooms[0].rpcCalls[0].subscribed);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '结束聊天' })));
  assert.equal(view.contexts[0].closes, 1);
  assert.equal(view.calls.renderers, 0);
  assert.equal(view.rooms.length, 1);
  assert.ok(view.getByRole('button', { name: '开始聊天' }));
});

/** 文字入口不能承诺收音；按钮和提示必须同时跟随实际轨道，而不是最初入口或开关意图。 */
test('文字入口及麦克风开关同步角色与页脚，不把静音或 disabled 轨道当作采集', async () => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  assert.equal(view.calls.microphones.length, 0);
  assert.ok(view.getByText('等你发来消息'));
  assert.ok(view.getByText('可以打字，也可以开启麦克风。'));
  assert.equal(view.queryByText(/说话时可以打断我/), null);
  assert.equal(view.getByRole('textbox').placeholder, '想聊什么，打字告诉我…');
  const room = view.rooms[0];
  await settle(() => room.setPeer({ ...room.peer, state: 'speaking' }));
  assert.ok(view.getByText('正在和你说话'));
  await settle(() => room.setPeer({ ...room.peer, state: 'thinking' }));
  assert.ok(view.getByText('让我想一想…'));
  await settle(() => room.setPeer({ ...room.peer, state: 'listening' }));
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.ok(view.getByText('正在听你说'));
  assert.ok(view.getByText('随时开口，也可以打字。说话时可以打断我。'));
  assert.ok(view.getByRole('button', { name: '关闭麦克风' }));
  assert.equal(view.getByRole('textbox').placeholder, '想说什么，也可以打字…');
  await settle(() => fireEvent.click(view.getByRole('button', { name: '关闭麦克风' })));
  assert.ok(view.getByText('等你发来消息'));
  const track = view.tracks[0];
  for (const [muted, enabled] of [
    [true, true],
    [false, false],
  ]) {
    await settle(() => {
      track.isMuted = muted;
      track.mediaStreamTrack.enabled = enabled;
      room.notify();
    });
    assert.ok(view.getByText('等你发来消息'));
    assert.ok(view.getByRole('button', { name: '开启麦克风' }));
    assert.equal(view.queryByText(/说话时可以打断我/), null);
  }
});

/** 原生轨道失效与同包装对象换轨不一定改变 SDK 对象身份，页面提示仍需跟随控件的唯一采集判定。 */
test('热拔出及同包装对象换轨同步收音提示，结束解除监听并隔离旧采集', async () => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '开始聊天' }));
  await waitFor(() => assert.ok(view.getByText('正在听你说')));
  const track = view.tracks[0];
  const previousMedia = track.mediaStreamTrack;
  await settle(() => {
    previousMedia.readyState = 'ended';
    previousMedia.dispatchEvent(new Event('ended'));
  });
  assert.ok(view.getByText('等你发来消息'));
  assert.ok(view.getByText('可以打字，也可以开启麦克风。'));
  await settle(() => {
    track.mediaStreamTrack = new LocalTrack().mediaStreamTrack;
    track.emit('restarted');
  });
  assert.ok(view.getByText('正在听你说'));
  assert.equal(track.listenerCount('restarted'), 1);
  await settle(() => previousMedia.dispatchEvent(new Event('ended')));
  assert.ok(view.getByText('正在听你说'));
  await settle(() => fireEvent.click(view.getByRole('button', { name: '结束聊天' })));
  assert.equal(track.listenerCount('restarted'), 0);
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  await settle(() => {
    track.mediaStreamTrack = new LocalTrack().mediaStreamTrack;
    track.emit('restarted');
    view.rooms[0].notify();
  });
  assert.ok(view.getByText('等你发来消息'));
  assert.equal(view.queryByText(/说话时可以打断我/), null);
});

/** 麦克风的存在不能覆盖连接许可，恢复与结束必须先说明当前不可继续聊天的边界。 */
test('连接恢复和结束文案优先于真实采集，正常结束再连接保留草稿且不自动发送', async () => {
  const ending = deferred();
  const view = await app({
    /** 仅延迟公开关闭端口，页面仍走真实取消、结束确认及资源回收流程。 */
    configure(room) {
      room.endGate = ending;
    },
  });
  fireEvent.click(view.getByRole('button', { name: '开始聊天' }));
  await waitFor(() => assert.ok(view.getByText('正在听你说')));
  fireEvent.change(view.getByRole('textbox'), { target: { value: '这是结束后需要保留的草稿。' } });
  const room = view.rooms[0];
  await settle(() => room.setState('reconnecting'));
  assert.ok(view.getByText('正在恢复连接…'));
  assert.ok(view.getByText('正在恢复连接，未发送的文字会保留。'));
  assert.equal(view.queryByText(/说话时可以打断我/), null);
  assert.equal(view.getByRole('textbox').disabled, true);
  await settle(() => room.setState('connected'));
  await waitFor(() => assert.ok(view.getByText('正在听你说')));
  await settle(() => fireEvent.click(view.getByRole('button', { name: '结束聊天' })));
  assert.ok(view.getByText('正在结束聊天…'));
  assert.ok(view.getByText('正在结束聊天，未发送的文字会保留。'));
  assert.equal(view.queryByText(/说话时可以打断我/), null);
  await settle(() => ending.resolve());
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  assert.equal(view.getByRole('textbox').value, '这是结束后需要保留的草稿。');
  assert.deepEqual(room.sent ?? [], []);
  assert.deepEqual(view.rooms[1].sent ?? [], []);
  assert.ok(view.getByText('等你发来消息'));
});

/** 开启意图和权限失败都不表示收音成功，文字会话必须保留可操作的错误与真实能力提示。 */
test('文字会话开启麦克风被拒绝后保留输入与关闭提示', async () => {
  const view = await app({
    /** 从浏览器权限端口拒绝，不能直接伪造控制器状态绕过采集判断。 */
    microphone: async () => {
      throw new DOMException('private failure', 'NotAllowedError');
    },
  });
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  fireEvent.change(view.getByRole('textbox'), { target: { value: '权限失败也不丢草稿' } });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.match(view.getByRole('alert').textContent, /权限被拒绝/);
  assert.equal(view.getByRole('textbox').value, '权限失败也不丢草稿');
  assert.equal(view.getByRole('textbox').disabled, false);
  assert.ok(view.getByRole('button', { name: '开启麦克风' }));
  assert.ok(view.getByText('等你发来消息'));
  assert.ok(view.getByText('可以打字，也可以开启麦克风。'));
  assert.equal(view.queryByText(/说话时可以打断我/), null);
});

/** 浏览器权限不可取消，取消后迟到的轨道必须停止且不能发布；下一次文字尝试仍可成功。 */
test('取消权限等待立即回收，迟到授权只停止旧采集', async () => {
  const permission = deferred();
  const track = new LocalTrack();
  const view = await app({ microphone: () => permission.promise });
  fireEvent.click(view.getByRole('button', { name: '开始聊天' }));
  await settle();
  assert.equal(view.calls.microphones.length, 1);
  assert.ok(view.getByText('正在连接，马上就好…'));
  assert.ok(view.getByText('正在连接，可以随时取消。'));
  assert.equal(view.queryByText(/说话时可以打断我/), null);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '取消连接' })));
  assert.equal(view.contexts[0].closes, 1);
  await settle(() => permission.resolve(track));
  assert.equal(track.stops, 1);
  assert.equal(view.rooms[0].published.length, 0);
  assert.equal(view.rooms[0].starts, 0);
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  assert.equal(view.rooms.length, 2);
});

/** SDK 令牌等待也可能无视取消，迟到连接只能释放原 Room，不能改变新尝试。 */
test('旧令牌结果迟到不会复活房间或覆盖新连接', async () => {
  const connection = deferred();
  const view = await app({
    /** 只延迟外部连接承诺，保留真实尝试对象对迟到结果的所有权。 */
    configure(room) {
      if (room.config && viewUnavailable(room)) room.startGate = connection;
    },
  });
  /** 每次 app 环境的新 Room 尚未启动，以构造参数不依赖 React 私有代次。 */
  function viewUnavailable(room) {
    return room.starts === 0 && connection.pending !== false;
  }
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await settle();
  const old = view.rooms[0];
  await settle(() => fireEvent.click(view.getByRole('button', { name: '取消连接' })));
  connection.pending = false;
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  await settle(() => connection.resolve());
  assert.equal(view.rooms.length, 2);
  assert.ok(old.disconnects >= 2);
  assert.equal(view.rooms[1].state, 'connected');
  assert.equal(view.contexts[1].state, 'running');
  assert.ok(view.getByRole('button', { name: '结束聊天' }));
});

/** 真实权限失败只显示安全提示，用户能从同一页面切换文字入口完成连接。 */
test('麦克风拒绝后提示可操作，文字连接不再次请求权限', async () => {
  const view = await app({
    microphone: async () => {
      throw new DOMException('private failure', 'NotAllowedError');
    },
  });
  fireEvent.click(view.getByRole('button', { name: '开始聊天' }));
  await waitFor(() => assert.match(view.getByRole('alert').textContent, /权限被拒绝/));
  assert.equal(view.contexts[0].closes, 1);
  fireEvent.click(view.getByRole('button', { name: '改用文字聊天' }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  assert.equal(view.calls.microphones.length, 1);
});

/** 不能将 RTC 连接成功当成聊天就绪；非法快照必须关闭资源并保留重试入口。 */
test('非法权威快照不开放输入或假装连接成功', async () => {
  const view = await app({
    /** 返回可解析但缺少许可字段的正文，隔离协议验证而非网络断开行为。 */
    configure(room) {
      room.rpcResponses = ['{"v":1}'];
    },
  });
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.match(view.getByRole('alert').textContent, /连接失败/));
  assert.equal(view.queryByRole('textbox'), null);
  assert.equal(view.contexts[0].closes, 1);
  assert.ok(view.getByRole('button', { name: '重新连接' }));
});

/** 网络恢复与 SID 换代都重新确认；等待期间保留草稿、挂断按钮并禁止发送。 */
test('重连共用快照，恢复不重发草稿且旧 SID 响应不能放行', async () => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  fireEvent.change(view.getByRole('textbox'), { target: { value: '恢复后仍待发送' } });
  const room = view.rooms[0];
  const gate = deferred();
  await settle(() => room.setState('reconnecting'));
  assert.equal(view.getByRole('textbox').disabled, true);
  assert.ok(view.getByRole('button', { name: '结束聊天' }));
  room.rpcGate = gate;
  await settle(() => {
    room.localParticipant.sid = 'PA_local_new';
    room.setPeer({ state: 'listening', agent: { ...view.agent, sid: 'PA_agent_new' } });
    room.setState('connected');
  });
  assert.equal(view.getByRole('textbox').disabled, true);
  assert.equal(room.rpcCalls.length, 2);
  await settle(() =>
    gate.resolve(
      JSON.stringify({
        v: 1,
        instance: 'job-a',
        revision: 1,
        reply_id: '',
        segment_id: '',
        state: 'closed',
        style: 'neutral',
        gesture: 'none',
      })
    )
  );
  assert.equal(view.getByRole('textbox').disabled, false);
  assert.equal(view.getByRole('textbox').value, '恢复后仍待发送');
  assert.deepEqual(room.sent ?? [], []);
  assert.equal(room.rpcCalls.length, 2);
});

/** 故障及用户结束都只更换会话宿主；草稿和历史留在页面，重连不能隐式丢弃或发送。 */
test('断连及正常结束后重连均保留草稿和历史，不自动重发', async () => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  fireEvent.change(view.getByRole('textbox'), { target: { value: '未发送的文字' } });
  await settle(() => {
    view.rooms[0].messages = [
      { id: 'history-a', message: '故障前的完整回复', timestamp: 0, from: view.agent },
    ];
    view.rooms[0].notify();
  });
  await settle(() => view.rooms[0].setState('disconnected'));
  assert.match(view.getByRole('alert').textContent, /已断开/);
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  assert.equal(view.getByRole('textbox').value, '未发送的文字');
  assert.ok(view.getByText('故障前的完整回复'));
  assert.deepEqual(view.rooms[1].sent ?? [], []);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '结束聊天' })));
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  assert.equal(view.getByRole('textbox').value, '未发送的文字');
  assert.ok(view.getByText('故障前的完整回复'));
  assert.deepEqual(view.rooms[2].sent ?? [], []);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '发送文字' })));
  assert.deepEqual(view.rooms[2].sent, ['未发送的文字']);
  assert.equal(view.getByRole('textbox').value, '');
});

/** 持久历史不能借由 Participant 持有旧房间，也不能随已退出的 SDK 对象变成另一位作者。 */
test('历史复制消息值，断连后 SDK 对象变化不能修改既有正文与作者', async () => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  const from = { ...view.agent, isLocal: false, signalClient: { retainedRoom: view.rooms[0] } };
  const message = { id: 'history-isolated', message: '这是一条已经显示的回复', timestamp: 0, from };
  await settle(() => {
    view.rooms[0].messages = [message];
    view.rooms[0].notify();
  });
  await settle(() => view.rooms[0].setState('disconnected'));
  from.isLocal = true;
  message.message = '已退出 SDK 的可变正文';
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  assert.ok(view.getByText('这是一条已经显示的回复').closest('.chat-row.assistant'));
  assert.equal(view.queryByText('已退出 SDK 的可变正文'), null);
});

/** 重复关闭期间不能建新尝试，迟到 ACK／关闭完成只释放旧房间自己的资源。 */
test('结束等待保持单个资源所有者，双击关闭不重复请求', async () => {
  const ending = deferred();
  const view = await app({
    /** 保留结束阶段的真实等待窗口，重复点击才能检验单一关闭所有者。 */
    configure(room) {
      room.endGate = ending;
    },
  });
  fireEvent.click(view.getByRole('button', { name: '开始聊天' }));
  await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
  await settle(() => fireEvent.click(view.getByRole('button', { name: '结束聊天' })));
  assert.ok(view.getByRole('button', { name: '正在结束…' }).disabled);
  assert.equal(view.tracks[0].stops, 1);
  assert.equal(view.rooms[0].ends, 1);
  await settle(() => ending.resolve());
  assert.equal(view.contexts[0].closes, 1);
  assert.equal(view.rooms[0].listenerCount(events.DataReceived), 0);
  assert.ok(view.getByRole('button', { name: '开始聊天' }));
});

/** 截止点不依赖测试机器速度；未完成的权限不会让连接永久占有时钟和页面。 */
test('连接三十秒到期回收资源，迟到权限不能重新发布', async (context) => {
  const permission = deferred();
  const track = new LocalTrack();
  const view = await app({ microphone: () => permission.promise });
  context.mock.timers.enable({ apis: ['setTimeout'] });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开始聊天' })));
  await settle(() => context.mock.timers.tick(29_999));
  assert.equal(view.queryByRole('alert'), null);
  assert.equal(view.contexts[0].closes, 0);
  await settle(() => context.mock.timers.tick(1));
  assert.match(view.getByRole('alert').textContent, /超过 30 秒/);
  assert.equal(view.contexts[0].closes, 1);
  await settle(() => permission.resolve(track));
  assert.equal(track.stops, 1);
  assert.equal(view.rooms[0].published.length, 0);
  context.mock.timers.reset();
});

/** 恢复期间来回切换 SDK 状态不延长预算，输入始终保持在用户手中。 */
test('恢复三十秒到期关闭，同一恢复过程不重置期限', async (context) => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  fireEvent.change(view.getByRole('textbox'), { target: { value: '连接恢复后自己发送' } });
  context.mock.timers.enable({ apis: ['setTimeout'] });
  await settle(() => view.rooms[0].setState('reconnecting'));
  await settle(() => context.mock.timers.tick(20_000));
  await settle(() => view.rooms[0].setState('signal-reconnecting'));
  await settle(() => context.mock.timers.tick(9_999));
  assert.equal(view.getByRole('textbox').disabled, true);
  assert.equal(view.contexts[0].closes, 0);
  await settle(() => context.mock.timers.tick(1));
  assert.match(view.getByRole('alert').textContent, /恢复连接超时/);
  assert.equal(view.contexts[0].closes, 1);
  assert.deepEqual(view.rooms[0].sent ?? [], []);
  context.mock.timers.reset();
});

/** SDK 会复用参与者包装对象，必须依赖 SID 值而非对象身份重新建立发送许可。 */
test('同一参与者对象换 SID 也重新确认，只有新快照允许输入', async () => {
  const view = await app();
  fireEvent.click(view.getByRole('button', { name: '用文字聊聊' }));
  await waitFor(() => assert.ok(view.getByRole('textbox')));
  const room = view.rooms[0];
  const confirmation = deferred();
  room.rpcGate = confirmation;
  await settle(() => {
    view.agent.sid = 'PA_agent_reused';
    room.notify();
  });
  assert.equal(room.rpcCalls.length, 2);
  assert.equal(view.getByRole('textbox').disabled, true);
  await settle(() =>
    confirmation.resolve(
      JSON.stringify({
        v: 1,
        instance: 'job-new',
        revision: 0,
        reply_id: '',
        segment_id: '',
        state: 'closed',
        style: 'neutral',
        gesture: 'none',
      })
    )
  );
  assert.equal(view.getByRole('textbox').disabled, false);
  assert.equal(room.rpcCalls.length, 2);
});

/** 完整真实 React 提交连续切换二十次，不能以分析器单测代替会话资源所有权验证。 */
test('连续二十次开始结束不遗留时钟、SDK 宿主或房间监听', async () => {
  const view = await app();
  for (let cycle = 0; cycle < 20; cycle++) {
    fireEvent.click(view.getByRole('button', { name: '开始聊天' }));
    await waitFor(() => assert.ok(view.getByRole('button', { name: '结束聊天' })));
    assert.equal(view.calls.renderers, 1);
    await settle(() => fireEvent.click(view.getByRole('button', { name: '结束聊天' })));
    const room = view.rooms[cycle];
    assert.equal(view.contexts[cycle].closes, 1);
    assert.equal(view.tracks[cycle].stops, 1);
    assert.equal(view.calls.renderers, 0);
    assert.equal(room.listeners.size, 0);
    for (const event of Object.values(events)) assert.equal(room.listenerCount(event), 0);
  }
  assert.equal(view.rooms.length, 20);
  assert.equal(view.contexts.length, 20);
  assert.ok(view.getByRole('button', { name: '开始聊天' }));
});
