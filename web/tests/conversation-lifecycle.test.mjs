import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const hookSource = await readFile(new URL('../hooks/use-conversation.ts', import.meta.url), 'utf8');
const hookCode = ts.transpileModule(hookSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const states = {
  Connected: 'connected',
  Disconnected: 'disconnected',
  Reconnecting: 'reconnecting',
  SignalReconnecting: 'signal-reconnecting',
};
const connectedEvent = 'connected';

/** 显式控制权限和 SDK 的完成顺序，测试不依赖网络、真实麦克风或脆弱的等待时长。 */
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

/** 等待真实 hook 的 Promise 链排空，不使用睡眠把竞态变成机器速度相关的偶然结果。 */
async function settle() {
  for (let turn = 0; turn < 12; turn++) await Promise.resolve();
}

/** 端口保留房间事件和采集所有权；只替换不可控的外部等待，不复制 hook 内部业务逻辑。 */
function createSession({ startGate, endGates = [] } = {}) {
  const calls = { start: 0, end: 0, disconnect: 0, published: [] };
  const room = new EventEmitter();
  room.state = states.Connected;
  room.localParticipant = {
    audioTrackPublications: new Map(),
    /** 已发布轨道仍由会话持有，便于验证挂断停止采集而非仅清空界面状态。 */
    async publishTrack(track, options) {
      calls.published.push({ track, options });
      this.audioTrackPublications.set(String(calls.published.length), { track });
    },
  };
  /** SDK 允许连接取消后迟到成功，测试必须观察 hook 对该事件的主动回收。 */
  room.disconnect = async () => {
    calls.disconnect++;
  };
  const session = {
    room,
    connectionState: states.Connected,
    calls,
    /** 忽略取消信号来模拟不可取消的 SDK 令牌请求，并保留公开 Connected 事件。 */
    async start() {
      calls.start++;
      if (startGate) await startGate.promise;
      room.emit(connectedEvent);
    },
    /** 每次 SDK 关闭可独立迟到，只有这样才能复现双击和失败清理交错的真实风险。 */
    async end() {
      const gate = endGates[calls.end++];
      if (gate) await gate.promise;
    },
  };
  return session;
}

/** 微型 React 端口保持状态、依赖和 effect 清理语义，业务代码始终来自当前真实 TS 源码。 */
function createHook({ session, resetRoom, microphone = async () => undefined }) {
  const slots = [];
  const timers = new Set();
  let cursor = 0;
  let mounted = true;
  let currentSession = session;
  let value;
  let writesAfterUnmount = 0;
  const startupAgent = {
    state: 'listening',
    /** Agent 就绪与连接相互独立；生命周期测试只控制房间与权限边界。 */
    async waitUntilConnected(signal) {
      signal.throwIfAborted();
    },
  };
  const assistant = { state: 'listening' };
  /** React 按 Object.is 比较依赖，避免伪端口因回调身份变化制造额外清理。 */
  function sameDependencies(left, right) {
    return Boolean(
      left && right && left.length === right.length && left.every((v, i) => Object.is(v, right[i]))
    );
  }
  const react = {
    /** Ref 的对象身份跨 render 持续存在，才能验证代次栅栏和共享关闭任务。 */
    useRef(initial) {
      const index = cursor++;
      return (slots[index] ??= { current: initial });
    },
    /** 异步更新保存到下一次显式 render；卸载后更新另计，不能静默掩盖资源复活。 */
    useState(initial) {
      const index = cursor++;
      const state = (slots[index] ??= { value: initial });
      return [
        state.value,
        (next) => {
          if (!mounted) writesAfterUnmount++;
          state.value = typeof next === 'function' ? next(state.value) : next;
        },
      ];
    },
    /** 保留 memo 依赖语义，effect 不因测试端口每次返回新函数而重复执行。 */
    useCallback(callback, dependencies) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || !sameDependencies(previous.dependencies, dependencies))
        slots[index] = { callback, dependencies };
      return slots[index].callback;
    },
    /** Effect 只在 render 提交后执行，清理先于新 effect，模拟 React 所有权转移。 */
    useEffect(effect, dependencies) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || !sameDependencies(previous.dependencies, dependencies))
        slots[index] = { effect, dependencies, cleanup: previous?.cleanup, pending: true };
    },
  };
  const ports = {
    react,
    'livekit-client': {
      ConnectionState: states,
      RoomEvent: { Connected: connectedEvent },
      Track: { Source: { Microphone: 'microphone' } },
      createLocalAudioTrack: microphone,
    },
    '@livekit/components-react': {
      useSessionContext: () => currentSession,
      useAgent: () => startupAgent,
      /** 实时参与者状态与首次等待器分开，可复现 SDK 历史 failed 在恢复后仍残留。 */
      useVoiceAssistant: () => assistant,
    },
    '@/lib/avatar/session-audio': { useSessionAudio: () => undefined },
  };
  const exports = {};
  const context = vm.createContext({
    exports,
    /** 请求模块只接受列出的内存端口，测试不会意外加载真实 SDK 并连接外部服务。 */
    require(name) {
      assert.ok(Object.hasOwn(ports, name), '未声明的外部依赖: ' + name);
      return ports[name];
    },
    AbortController,
    DOMException,
    Error,
    /** 超时由测试显式持有，不在 Node 测试结束后遗留 30 秒的活动句柄。 */
    setTimeout(callback, milliseconds) {
      const timer = { callback, milliseconds };
      timers.add(timer);
      return timer;
    },
    /** 清理所有权按句柄验证，而非依赖虚拟计时器自动消失。 */
    clearTimeout(timer) {
      timers.delete(timer);
    },
  });
  new vm.Script(hookCode, { filename: 'use-conversation.ts' }).runInContext(context);
  const harness = {
    /** 外部 provider 更换房间时更新当前值，旧回调仍必须持有原会话而不能碰新资源。 */
    replaceSession(next) {
      currentSession = next;
    },
    /** SDK 的可变 room.state 可以先于 React context 提交，覆盖参与者先断开的真实事件顺序。 */
    setConnectionState(state, roomState = state) {
      currentSession.connectionState = state;
      currentSession.room.state = roomState;
    },
    /** 模拟公开参与者状态，同时允许初始等待 hook 保留 SDK 的历史失败标记。 */
    setAgentState(state, startupState = state) {
      assistant.state = state;
      startupAgent.state = startupState;
    },
    /** 返回当前计时器以测试窗口是否跨越不同恢复模式保持同一截止点。 */
    pendingTimers() {
      return [...timers];
    },
    /** 显式执行计时器，可重放已进入事件队列的旧回调，不依赖真实 30 秒等待。 */
    fireTimer(timer) {
      timers.delete(timer);
      timer.callback();
    },
    /** 显式提交避免用无限自动重渲染掩盖竞态，断言前可准确读到实际 hook 状态。 */
    render() {
      assert.ok(mounted, '卸载的 hook 不能重新 render');
      cursor = 0;
      value = exports.useConversation(resetRoom);
      for (const slot of slots) {
        if (slot?.pending) {
          slot.pending = false;
          slot.cleanup?.();
          slot.cleanup = slot.effect();
        }
      }
      return value;
    },
    /** 卸载只运行真实 effect 的清理，迟到 Promise 是否越界由业务代码自己决定。 */
    unmount() {
      mounted = false;
      for (const slot of slots) slot?.cleanup?.();
    },
    /** 生命周期计数可暴露卸载后 setState 与未清除的连接超时句柄。 */
    diagnostics() {
      return { writesAfterUnmount, timers: timers.size };
    },
  };
  harness.render();
  return harness;
}

/** 双击挂断共用同一 Promise，重连必须等清理完成，避免第二个迟到 finally 重置新房间。 */
test('挂断共享一次任务，清理中不能开始新连接，旧调用完成后新会话保持 active', async () => {
  const closing = deferred();
  const firstSession = createSession({ endGates: [closing] });
  const nextSession = createSession();
  let resets = 0;
  const hook = createHook({
    session: firstSession,
    /** Provider 重建拥有新资源的房间，旧关闭任务不得再次触发这个所有权操作。 */
    resetRoom() {
      resets++;
      hook.replaceSession(nextSession);
    },
  });
  await hook.render().start(false);
  assert.equal(hook.render().phase, 'active');
  const firstEnd = hook.render().end();
  const secondEnd = hook.render().end();
  assert.equal(firstEnd, secondEnd);
  assert.equal(firstSession.calls.end, 1);
  assert.equal(hook.render().phase, 'ending');
  await hook.render().start(false);
  assert.equal(firstSession.calls.start, 1);
  closing.resolve();
  await firstEnd;
  assert.equal(resets, 1);
  assert.equal(hook.render().phase, 'idle');
  await hook.render().start(false);
  await secondEnd;
  assert.equal(nextSession.calls.start, 1);
  assert.equal(hook.render().phase, 'active');
  assert.equal(resets, 1);
  hook.unmount();
  await settle();
  assert.equal(hook.diagnostics().timers, 0);
});

/** 旧舞台卸载会递增代次，关闭 Promise 即使后到也不能重建当前 provider 的新房间。 */
test('卸载前开始的关闭迟到完成，不调用 resetRoom 或更新已卸载状态', async () => {
  const closing = deferred();
  const session = createSession({ endGates: [closing] });
  let resets = 0;
  const hook = createHook({ session, resetRoom: () => resets++ });
  await hook.render().start(false);
  const ending = hook.render().end();
  hook.unmount();
  closing.resolve();
  await ending;
  await settle();
  assert.equal(resets, 0);
  assert.deepEqual(hook.diagnostics(), { writesAfterUnmount: 0, timers: 0 });
});

/** SDK 连接不可取消时可能在卸载后发 Connected，必须断开旧房间并移除监听而不能复活。 */
test('卸载后迟到连接成功会回收旧房间，不发布轨道、不重置新资源', async () => {
  const connecting = deferred();
  const session = createSession({ startGate: connecting });
  let resets = 0;
  const hook = createHook({ session, resetRoom: () => resets++ });
  const starting = hook.render().start(false);
  await settle();
  assert.equal(session.calls.start, 1);
  hook.unmount();
  await starting;
  connecting.resolve();
  await settle();
  assert.ok(session.calls.disconnect >= 1);
  assert.equal(session.room.listenerCount(connectedEvent), 0);
  assert.equal(session.calls.published.length, 0);
  assert.equal(resets, 0);
  assert.deepEqual(hook.diagnostics(), { writesAfterUnmount: 0, timers: 0 });
});

/** 浏览器权限不能取消，旧权限结果到达时停止其采集，不能误停取消后建立的文字会话。 */
test('取消后迟到的麦克风授权会 stop，旧尝试不连接或发布到新房间', async () => {
  const permission = deferred();
  const oldSession = createSession();
  const nextSession = createSession();
  let resets = 0;
  let stops = 0;
  const hook = createHook({
    session: oldSession,
    microphone: () => permission.promise,
    /** 模拟正常取消后 provider 换房，迟到授权仍由旧 controller 负责停止。 */
    resetRoom() {
      resets++;
      hook.replaceSession(nextSession);
    },
  });
  const oldStart = hook.render().start();
  await hook.render().end();
  await oldStart;
  assert.equal(hook.render().phase, 'idle');
  await hook.render().start(false);
  assert.equal(hook.render().phase, 'active');
  permission.resolve({ stop: () => stops++ });
  await settle();
  assert.equal(stops, 1);
  assert.equal(oldSession.calls.start, 0);
  assert.equal(oldSession.calls.published.length, 0);
  assert.equal(nextSession.calls.start, 1);
  assert.equal(hook.render().phase, 'active');
  assert.equal(resets, 1);
  assert.equal(oldSession.room.listenerCount(connectedEvent), 0);
  hook.unmount();
  await settle();
  assert.equal(hook.diagnostics().timers, 0);
});

/** 启动失败自己的 SDK 回收也可能迟到，手动取消并重连后旧 catch 不得再 resetRoom。 */
test('启动失败的迟到清理不能重置取消后成功建立的新会话', async () => {
  const connecting = deferred();
  const failureCleanup = deferred();
  const manualCleanup = deferred();
  const oldSession = createSession({
    startGate: connecting,
    endGates: [failureCleanup, manualCleanup],
  });
  const nextSession = createSession();
  let resets = 0;
  const hook = createHook({
    session: oldSession,
    /** 同一 provider 在失败与手动取消交错时只能转移一次所有权。 */
    resetRoom() {
      resets++;
      hook.replaceSession(nextSession);
    },
  });
  const failedStart = hook.render().start(false);
  connecting.reject(new Error('模拟令牌请求失败'));
  await settle();
  assert.equal(oldSession.calls.end, 1);
  const manualEnd = hook.render().end();
  assert.equal(oldSession.calls.end, 2);
  manualCleanup.resolve();
  await manualEnd;
  await hook.render().start(false);
  assert.equal(hook.render().phase, 'active');
  assert.equal(nextSession.calls.start, 1);
  failureCleanup.resolve();
  await failedStart;
  assert.equal(resets, 1);
  assert.equal(hook.render().phase, 'active');
  hook.unmount();
  await settle();
  assert.equal(hook.diagnostics().timers, 0);
});

/** 完整重连先移除 Agent，再提交房间状态；恢复过程中和恢复后的 SDK 历史 failed 都不能挂断。 */
test('信令与完整重连保留 active，Agent 返回后清除等待且使用实时公开状态', async () => {
  for (const mode of [states.Reconnecting, states.SignalReconnecting]) {
    const session = createSession();
    let resets = 0;
    const hook = createHook({
      session,
      /** 意外 reset 会使真实浏览器回到欢迎页，必须作为测试失败观察。 */
      resetRoom: () => resets++,
    });
    await hook.render().start(false);
    assert.equal(hook.render().phase, 'active');
    hook.setAgentState('disconnected', 'failed');
    hook.setConnectionState(states.Connected, mode);
    assert.equal(hook.render().reconnecting, true);
    assert.equal(hook.render().phase, 'active');
    assert.equal(session.calls.end, 0);
    const timer = hook.pendingTimers()[0];
    assert.equal(timer.milliseconds, 30_000);
    hook.setConnectionState(mode);
    hook.setAgentState('connecting', 'failed');
    hook.render();
    assert.equal(hook.pendingTimers()[0], timer);
    hook.setConnectionState(states.Connected);
    assert.equal(hook.render().reconnecting, true);
    assert.equal(hook.pendingTimers()[0], timer);
    // 实际 SDK 已恢复而 React context 仍滞后时，也不能让到期的旧超时把新连接关闭。
    hook.setConnectionState(mode, states.Connected);
    hook.setAgentState('thinking', 'failed');
    assert.equal(hook.render().reconnecting, false);
    assert.equal(hook.render().agent.state, 'thinking');
    assert.equal(hook.render().phase, 'active');
    assert.equal(hook.pendingTimers().length, 0);
    hook.fireTimer(timer);
    await settle();
    assert.equal(session.calls.end, 0);
    assert.equal(resets, 0);
    assert.equal(hook.render().error, '');
    hook.unmount();
    await settle();
    assert.equal(hook.diagnostics().timers, 0);
  }
});

/** 信令恢复升级为完整重连不能重新获得 30 秒，房间已恢复但 Agent 未归来也算同一恢复窗口。 */
test('恢复窗口跨越模式切换保持同一截止点，超时才回收会话并显示恢复操作', async () => {
  const session = createSession();
  let resets = 0;
  const hook = createHook({
    session,
    /** 一次恢复失败只转移一次房间所有权，重复超时不得二次重建。 */
    resetRoom: () => resets++,
  });
  await hook.render().start(false);
  hook.setConnectionState(states.SignalReconnecting);
  hook.setAgentState('connecting', 'failed');
  assert.equal(hook.render().phase, 'active');
  const timer = hook.pendingTimers()[0];
  hook.setConnectionState(states.Reconnecting);
  assert.equal(hook.render().reconnecting, true);
  assert.equal(hook.pendingTimers()[0], timer);
  hook.setConnectionState(states.Connected);
  hook.render();
  assert.equal(hook.pendingTimers()[0], timer);
  assert.equal(session.calls.end, 0);
  hook.fireTimer(timer);
  await settle();
  assert.equal(session.calls.end, 1);
  assert.equal(resets, 1);
  assert.equal(hook.render().phase, 'idle');
  assert.match(hook.render().error, /恢复连接超时/);
  hook.fireTimer(timer);
  await settle();
  assert.equal(session.calls.end, 1);
  hook.unmount();
  await settle();
  assert.equal(hook.diagnostics().timers, 0);
});

/** SDK 明确最终断开应立即结束，不能继续等待超时；React 状态落后时也读取公共 room.state。 */
test('恢复失败的最终 Disconnected 立即结束，不等待 30 秒也不留下旧计时器', async () => {
  const session = createSession();
  let resets = 0;
  const hook = createHook({ session, resetRoom: () => resets++ });
  await hook.render().start(false);
  hook.setConnectionState(states.Reconnecting);
  hook.setAgentState('connecting', 'failed');
  hook.render();
  const timer = hook.pendingTimers()[0];
  hook.setConnectionState(states.Reconnecting, states.Disconnected);
  hook.render();
  await settle();
  assert.equal(session.calls.end, 1);
  assert.equal(resets, 1);
  assert.equal(hook.render().phase, 'idle');
  assert.match(hook.render().error, /聊天连接已断开/);
  assert.equal(hook.pendingTimers().length, 0);
  hook.fireTimer(timer);
  await settle();
  assert.equal(session.calls.end, 1);
  hook.unmount();
  await settle();
  assert.equal(hook.diagnostics().timers, 0);
});
