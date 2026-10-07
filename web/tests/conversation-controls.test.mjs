import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

/** 测试执行当前组件和错误映射，只替换 React 与不可控设备端口，不复制恢复逻辑。 */
async function compileSource(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
}
const componentCode = await compileSource('../components/app/conversation-controls.tsx');
const conversationCode = await compileSource('../hooks/use-conversation.ts');

/** 权限与发布分别可迟到，避免依赖真实浏览器时序或固定睡眠。 */
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

/** stop 不触发 ended，与浏览器语义一致，防止测试端口代替组件修正过期状态。 */
function createMediaTrack(enabled = true) {
  const media = new EventTarget();
  media.readyState = 'live';
  media.enabled = enabled;
  const listeners = new Map();
  const addListener = media.addEventListener.bind(media);
  const removeListener = media.removeEventListener.bind(media);
  /** 保留真实 EventTarget 分发，同时单独核对监听器所有权，避免 mounted 守卫掩盖泄漏。 */
  media.addEventListener = (type, listener) => {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(listener);
    addListener(type, listener);
  };
  /** 清理必须匹配同一监听器身份，不能只以卸载后没有状态更新推断资源已释放。 */
  media.removeEventListener = (type, listener) => {
    listeners.get(type)?.delete(listener);
    removeListener(type, listener);
  };
  /** 仅作为资源计数证据，不向组件暴露浏览器实际不存在的恢复方法。 */
  media.listenerCount = (type) => listeners.get(type)?.size ?? 0;
  return media;
}

/** publication 可保留未静音的 ended 轨道，复现 SDK 设备切换失败后的真实状态。 */
function createTrack({ muted = false, ended = false } = {}) {
  const track = new EventEmitter();
  track.isMuted = muted;
  track.mediaStreamTrack = createMediaTrack(!muted);
  if (ended) track.mediaStreamTrack.readyState = 'ended';
  track.stops = 0;
  /** 显式统计采集释放，不能只验证页面文字变成关闭。 */
  track.stop = () => {
    track.stops++;
    track.mediaStreamTrack.readyState = 'ended';
  };
  return track;
}

/** 内存 SDK 仅保留公共 API 和失败行为，房间切换后的恢复决策仍由真实组件执行。 */
function createRoom(track) {
  const calls = { switch: [], unpublish: [], publish: [], enabled: [] };
  const room = { state: 'connected', activeDeviceId: 'device-a', calls };
  const participant = {
    publication: track ? { audioTrack: track } : undefined,
    /** 查询不自动丢弃 ended 轨道，与 SDK 的 publication 生命周期一致。 */
    getTrackPublication() {
      return this.publication;
    },
    /** 公开取消发布同时停止采集，组件不得修改 SDK 的内部设备映射。 */
    async unpublishTrack(current) {
      calls.unpublish.push(current);
      current.stop();
      if (this.publication?.audioTrack === current) this.publication = undefined;
    },
    /** 发布控制点允许组件卸载后才成功，用来验证迟到成功的主动回收。 */
    async publishTrack(current) {
      if (room.publishGate) await room.publishGate.promise;
      calls.publish.push(current);
      this.publication = { audioTrack: current };
    },
    /** 开关仅改变已持有轨道，不制造组件自己的失败恢复行为。 */
    async setMicrophoneEnabled(enabled) {
      calls.enabled.push(enabled);
      const current = this.publication?.audioTrack;
      if (room.enableFailure) {
        current.stop();
        throw room.enableFailure;
      }
      current.isMuted = !enabled;
      current.mediaStreamTrack.enabled = enabled;
    },
  };
  room.localParticipant = participant;
  /** 唯一选中设备来自公开 room 状态，恢复必须调用公开切换接口。 */
  room.getActiveDevice = () => room.activeDeviceId;
  /** 模拟 SDK 先停旧轨道再采集的顺序；错误后不自动修复 publication。 */
  room.switchActiveDevice = async (kind, deviceId) => {
    assert.equal(kind, 'audioinput');
    calls.switch.push(deviceId);
    if (room.switchFailure && deviceId === 'device-b') {
      participant.publication?.audioTrack.stop();
      if (room.switchGate) await room.switchGate.promise;
      throw room.switchFailure;
    }
    if (room.switchResult === false && deviceId === 'device-b') return false;
    room.activeDeviceId = deviceId;
    return true;
  };
  return room;
}

/** 显式提交 render 和 effect，保留 ref、异步状态与卸载清理，避免静态源码断言。 */
function createControls({
  room,
  // 默认端口只分配内存轨道，测试不会向真实麦克风请求权限。
  acquire = async () => createTrack(),
  // 默认文字端口没有网络依赖，失败测试自行控制外部交付结果。
  send = async () => undefined,
}) {
  const slots = [];
  let cursor = 0;
  let mounted = true;
  let writesAfterUnmount = 0;
  let tree;
  let ready = true;
  const acquisitions = [];
  /** React 的依赖比较按引用身份进行，真实监听器必须随换轨而清理重建。 */
  function sameDependencies(left, right) {
    return Boolean(
      left &&
        right &&
        left.length === right.length &&
        // Object.is 保留 ref 身份语义，避免把 SDK 可变轨道当成新的对象。
        left.every((value, index) => Object.is(value, right[index]))
    );
  }
  const react = {
    /** 实际设备互斥边界必须跨 render 存活，不能由测试重新创建。 */
    useRef(initial) {
      return (slots[cursor++] ??= { current: initial });
    },
    /** 记录卸载后的更新；迟到设备结果应只释放资源而不恢复界面。 */
    useState(initial) {
      const state = (slots[cursor++] ??= { value: initial });
      return [
        state.value,
        // 执行真实组件的更新器，保留草稿比较与单调的监听器修订号。
        (next) => {
          if (!mounted) writesAfterUnmount++;
          state.value = typeof next === 'function' ? next(state.value) : next;
        },
      ];
    },
    /** 错误回调的稳定身份避免枚举端口因每次 render 重建而伪造失败。 */
    useCallback(callback, dependencies) {
      const index = cursor++;
      if (!sameDependencies(slots[index]?.dependencies, dependencies))
        slots[index] = { callback, dependencies };
      return slots[index].callback;
    },
    /** effect 提交时先清理旧所有权，确保原生轨道监听器不会残留。 */
    useEffect(effect, dependencies) {
      const index = cursor++;
      const previous = slots[index];
      if (!sameDependencies(previous?.dependencies, dependencies))
        slots[index] = { effect, dependencies, cleanup: previous?.cleanup, pending: true };
    },
  };
  /** JSX 保留事件及可访问属性，测试检查用户实际可点击的控件输出。 */
  function jsx(type, props) {
    return { type, props };
  }
  const ports = {
    react,
    'react/jsx-runtime': { jsx, jsxs: jsx },
    'livekit-client': {
      Track: { Source: { Microphone: 'microphone' } },
      TrackEvent: { Restarted: 'restarted' },
      /** 只控制权限结果，发布、收回和界面仍使用真实组件代码。 */
      async createLocalAudioTrack(options) {
        acquisitions.push(options);
        return acquire(options);
      },
    },
    'lucide-react': {
      ChevronDown: 'icon-chevron',
      LoaderCircle: 'icon-loading',
      Mic: 'icon-mic',
      MicOff: 'icon-mic-off',
      PhoneOff: 'icon-phone-off',
      Send: 'icon-send',
    },
    '@livekit/components-react': {
      /** 每次读取同一个房间，旧异步回调只能处理它原先持有的资源。 */
      useSessionContext: () => ({ room }),
      /** SDK enabled 故意只检查静音标记，组件需自行检查真实 readyState。 */
      useTrackToggle: () => ({
        track: room.localParticipant.publication,
        enabled: !room.localParticipant.publication?.audioTrack.isMuted,
        pending: false,
      }),
      /** 设备选择只从公共 API 更新；测试不提供可写内部映射。 */
      useMediaDeviceSelect: () => ({
        activeDeviceId: room.activeDeviceId,
        devices: [
          { deviceId: 'device-a', label: '麦克风 A' },
          { deviceId: 'device-b', label: '麦克风 B' },
        ],
      }),
    },
    '@/components/ui/button': { Button: 'button' },
    '@/lib/avatar/session-audio': {},
  };
  /** 当前源码在隔离 VM 内执行，未知依赖会失败，测试不能访问网络或真实设备。 */
  function evaluate(code, filename) {
    const exports = {};
    const context = vm.createContext({
      exports,
      Error,
      // 外部端口必须显式声明，防止模块变更悄悄引入真实浏览器或 SDK 生命周期。
      require(name) {
        assert.ok(Object.hasOwn(ports, name), '未声明的外部依赖: ' + name);
        return ports[name];
      },
    });
    new vm.Script(code, { filename }).runInContext(context);
    return exports;
  }
  ports['@/hooks/use-conversation'] = evaluate(conversationCode, 'use-conversation.ts');
  const component = evaluate(componentCode, 'conversation-controls.tsx');
  const harness = {
    acquisitions,
    /** 更新可用状态仅用于模拟重连；无需重复加载或替换当前组件。 */
    setReady(value) {
      ready = value;
    },
    /** 显式提交确保异步成功和失败不会凭测试自动改写当前树。 */
    render() {
      cursor = 0;
      tree = component.ConversationControls({
        ready,
        sending: false,
        send,
        // 挂断归上层生命周期负责，测试在迟到场景显式卸载并改变房间状态。
        end: async () => undefined,
      });
      for (const slot of slots) {
        if (!slot?.pending) continue;
        slot.cleanup?.();
        slot.cleanup = slot.effect();
        slot.pending = false;
      }
      return tree;
    },
    /** 递归收集真实 JSX，不调用或复制组件事件处理逻辑。 */
    elements() {
      const values = [];
      /** 数组子节点和标签节点都可有嵌套内容，保留所有 alert 以发现相互遮挡。 */
      function visit(node) {
        if (Array.isArray(node)) {
          node.forEach(visit);
        } else if (node && typeof node === 'object') {
          values.push(node);
          visit(node.props?.children);
        }
      }
      visit(tree);
      return values;
    },
    /** 按用户可访问标签定位按钮，避免以函数名称绕过 UI 和事件绑定。 */
    element(type, label) {
      return this.elements().find(
        // 未提供 label 时选中唯一标签，例如 textarea 或设备 select。
        (node) => node.type === type && (!label || node.props['aria-label'] === label)
      );
    },
    /** 返回独立错误文本，设备恢复不能顺带清除尚未发送成功的草稿错误。 */
    alerts() {
      return this.elements()
        .filter(
          // role 是最终 JSX 的无障碍契约，而不是测试自定义的错误存储。
          (node) => node.props.role === 'alert'
        )
        .map(
          // 两个错误在各自的 alert 中完整可见，不能只检查条件为真。
          (node) => node.props.children
        );
    },
    /** 清理 effect 后再让权限或发布结果完成，复现页面退出时的真实所有权边界。 */
    unmount() {
      for (const slot of slots) slot?.cleanup?.();
      mounted = false;
    },
    /** 卸载后异步错误不能写入旧组件，更不能污染下一会话。 */
    get writesAfterUnmount() {
      return writesAfterUnmount;
    },
  };
  harness.render();
  return harness;
}

/** 失败切换需恢复选择、释放失效 publication，并让下一次点击直接恢复真实采集。 */
test('failed device switch cleans ended track and retries microphone in one click', async () => {
  const previous = createTrack();
  const recovered = createTrack();
  const room = createRoom(previous);
  room.switchFailure = new DOMException('device busy', 'NotReadableError');
  const controls = createControls({
    room,
    // 恢复必须重新采集，不能让未静音的旧 ended 轨道伪装成功。
    acquire: async () => recovered,
  });
  await controls.element('select').props.onChange({ target: { value: 'device-b' } });
  // JSX onChange 有意不阻塞浏览器事件循环，等待其 Promise 链完成后显式提交。
  for (let turn = 0; turn < 12; turn++) await Promise.resolve();
  controls.render();
  assert.deepEqual(room.calls.switch, ['device-b', 'device-a']);
  assert.deepEqual(room.calls.unpublish, [previous]);
  assert.equal(room.localParticipant.publication, undefined);
  assert.equal(controls.element('select').props.value, 'device-a');
  assert.equal(controls.element('button', '开启麦克风').props['aria-pressed'], false);
  assert.match(controls.alerts()[0], /麦克风暂不可用/);
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.equal(controls.acquisitions[0].deviceId, 'device-a');
  assert.deepEqual(room.calls.publish, [recovered]);
  assert.equal(controls.element('button', '关闭麦克风').props['aria-pressed'], true);
  assert.deepEqual(controls.alerts(), []);
});

/** 无事件的 SDK stop 仍需在 render 时读取 readyState，不能只相信 enabled 缓存。 */
test('ended unmuted publication is displayed closed and recreated directly', async () => {
  const previous = createTrack({ ended: true });
  const room = createRoom(previous);
  const controls = createControls({ room });
  assert.equal(controls.element('button', '开启麦克风').props['aria-pressed'], false);
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.deepEqual(room.calls.unpublish, [previous]);
  assert.equal(room.calls.enabled.length, 0);
  assert.equal(room.calls.publish.length, 1);
  assert.equal(controls.element('button', '关闭麦克风').props['aria-pressed'], true);
});

/** 忽略同一 render 的重复事件，防止失败恢复把后一次选择覆盖成旧设备。 */
test('device selection and microphone toggle share one in-flight boundary', async () => {
  const room = createRoom(createTrack());
  room.switchFailure = new DOMException('device busy', 'NotReadableError');
  room.switchGate = deferred();
  const controls = createControls({ room });
  const select = controls.element('select');
  const microphone = controls.element('button', '关闭麦克风');
  select.props.onChange({ target: { value: 'device-b' } });
  select.props.onChange({ target: { value: 'device-a' } });
  await microphone.props.onClick();
  controls.render();
  assert.equal(controls.element('select').props.disabled, true);
  assert.equal(controls.element('button', '开启麦克风').props['aria-busy'], true);
  assert.deepEqual(room.calls.switch, ['device-b']);
  assert.equal(room.calls.enabled.length, 0);
  room.switchGate.resolve();
  for (let turn = 0; turn < 12; turn++) await Promise.resolve();
  controls.render();
  assert.deepEqual(room.calls.switch, ['device-b', 'device-a']);
  assert.equal(controls.element('select').props.disabled, false);
});

/** SDK 可返回 false 而非拒绝，设备选择必须显式把它当成失败并保持恢复入口。 */
test('unsuccessful device result is reported and selection restored', async () => {
  const room = createRoom();
  room.switchResult = false;
  const controls = createControls({ room });
  controls.element('select').props.onChange({ target: { value: 'device-b' } });
  for (let turn = 0; turn < 12; turn++) await Promise.resolve();
  controls.render();
  assert.deepEqual(room.calls.switch, ['device-b', 'device-a']);
  assert.equal(controls.element('select').props.value, 'device-a');
  assert.match(controls.alerts()[0], /麦克风暂不可用/);
});

/** 重新开启也可能在 SDK 换轨阶段失败，结束的 publication 同样必须退出并允许一键重试。 */
test('failed unmute clears ended track before one-click recovery', async () => {
  const previous = createTrack({ muted: true });
  const room = createRoom(previous);
  room.enableFailure = new DOMException('denied', 'NotAllowedError');
  const controls = createControls({ room });
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.deepEqual(room.calls.unpublish, [previous]);
  assert.match(controls.alerts()[0], /权限被拒绝/);
  room.enableFailure = undefined;
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.equal(room.calls.publish.length, 1);
  assert.equal(controls.element('button', '关闭麦克风').props['aria-pressed'], true);
});

/** SDK 在取消发布后协商失败仍已释放 publication，不能让这类异常阻塞正常一键恢复。 */
test('unpublish negotiation failure after removal still permits microphone recovery', async () => {
  const previous = createTrack({ ended: true });
  const room = createRoom(previous);
  const unpublish = room.localParticipant.unpublishTrack.bind(room.localParticipant);
  /** 保留公开取消发布的真实副作用，错误只来自后续协商。 */
  room.localParticipant.unpublishTrack = async (track) => {
    await unpublish(track);
    throw new Error('negotiation failed');
  };
  const controls = createControls({ room });
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.deepEqual(room.calls.unpublish, [previous]);
  assert.equal(room.calls.publish.length, 1);
  assert.equal(controls.element('button', '关闭麦克风').props['aria-pressed'], true);
  assert.deepEqual(controls.alerts(), []);
});

/** 若公共取消发布尚未移除旧轨道，不可重复发布；待 SDK 恢复后同一按钮能正常重试。 */
test('retained ended publication prevents duplicate capture and can be retried', async () => {
  const previous = createTrack({ ended: true });
  const room = createRoom(previous);
  const unpublish = room.localParticipant.unpublishTrack.bind(room.localParticipant);
  let removeFails = true;
  /** 模拟发布 Promise 在 map 清理前失败，不能让组件误以为旧 publication 已退出。 */
  room.localParticipant.unpublishTrack = async (track) => {
    if (removeFails) throw new Error('publication not removed');
    return unpublish(track);
  };
  const controls = createControls({ room });
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.equal(controls.acquisitions.length, 0);
  assert.equal(room.calls.publish.length, 0);
  assert.equal(controls.element('button', '开启麦克风').props['aria-pressed'], false);
  assert.match(controls.alerts()[0], /麦克风暂不可用/);
  removeFails = false;
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.deepEqual(room.calls.unpublish, [previous]);
  assert.equal(room.calls.publish.length, 1);
  assert.equal(controls.element('button', '关闭麦克风').props['aria-pressed'], true);
});

/** 麦克风权限无法取消，迟到授权只能停止采集，不得发布到已经结束的房间。 */
test('late permission after unmount releases capture without publication or stale state', async () => {
  const gate = deferred();
  const track = createTrack();
  const room = createRoom();
  const controls = createControls({
    room,
    // 外部授权保留到卸载之后，组件仍须主动处理其结果。
    acquire: () => gate.promise,
  });
  const opening = controls.element('button', '开启麦克风').props.onClick();
  await Promise.resolve();
  controls.unmount();
  room.state = 'disconnected';
  gate.resolve(track);
  await opening;
  assert.equal(track.stops, 1);
  assert.equal(room.calls.publish.length, 0);
  assert.equal(controls.writesAfterUnmount, 0);
});

/** 权限完成不代表发布完成，挂断途中迟到发布成功也必须撤回并停止其本地轨道。 */
test('late publication after unmount is unpublished and stopped', async () => {
  const room = createRoom();
  room.publishGate = deferred();
  const track = createTrack();
  const controls = createControls({
    room,
    // 发布单独受控，防止只验证权限前的取消而漏掉第二个异步边界。
    acquire: async () => track,
  });
  const opening = controls.element('button', '开启麦克风').props.onClick();
  for (let turn = 0; turn < 5; turn++) await Promise.resolve();
  controls.unmount();
  room.state = 'disconnected';
  room.publishGate.resolve();
  await opening;
  assert.deepEqual(room.calls.unpublish, [track]);
  assert.equal(track.mediaStreamTrack.readyState, 'ended');
  assert.equal(room.localParticipant.publication, undefined);
  assert.equal(controls.writesAfterUnmount, 0);
});

/** 失败设备结果若在挂断后抵达，只回收旧房间，不能再恢复设备或写入错误。 */
test('late device failure after unmount cleans track without restoring selection', async () => {
  const previous = createTrack();
  const room = createRoom(previous);
  room.switchFailure = new DOMException('device busy', 'NotReadableError');
  room.switchGate = deferred();
  const controls = createControls({ room });
  controls.element('select').props.onChange({ target: { value: 'device-b' } });
  controls.unmount();
  room.state = 'disconnected';
  room.switchGate.resolve();
  for (let turn = 0; turn < 12; turn++) await Promise.resolve();
  assert.deepEqual(room.calls.unpublish, [previous]);
  assert.deepEqual(room.calls.switch, ['device-b']);
  assert.equal(controls.writesAfterUnmount, 0);
});

/** 两个并行用户动作各有恢复路径，任一成功不能遮挡另一失败或丢掉待发送草稿。 */
test('message and device errors remain independently visible through recovery', async () => {
  let sendFails = true;
  const sent = [];
  const room = createRoom();
  const controls = createControls({
    room,
    // 发送成功只表示交付完成，不替组件清空草稿或错误。
    send: async (message) => {
      sent.push(message);
      if (sendFails) throw new Error('send failed');
    },
    // 无可用麦克风仍应完整保留文字聊天与失败重试。
    acquire: async () => {
      throw new DOMException('denied', 'NotAllowedError');
    },
  });
  controls.element('textarea').props.onChange({ target: { value: '你好，小芽' } });
  controls.render();
  controls.element('form').props.onSubmit({
    // form 保留浏览器禁止默认跳转的契约，发送异步工作由组件自身启动。
    preventDefault() {},
  });
  for (let turn = 0; turn < 8; turn++) await Promise.resolve();
  controls.render();
  await controls.element('button', '开启麦克风').props.onClick();
  controls.render();
  assert.equal(controls.alerts().length, 2);
  assert.match(controls.alerts()[0], /发送失败/);
  assert.match(controls.alerts()[1], /权限被拒绝/);
  assert.equal(controls.element('textarea').props.value, '你好，小芽');
  sendFails = false;
  controls.element('form').props.onSubmit({
    // 通过用户的同一发送入口重试，不能直接调用组件内部 submit。
    preventDefault() {},
  });
  for (let turn = 0; turn < 8; turn++) await Promise.resolve();
  controls.render();
  assert.deepEqual(sent, ['你好，小芽', '你好，小芽']);
  assert.equal(controls.element('textarea').props.value, '');
  assert.equal(controls.alerts().length, 1);
  assert.match(controls.alerts()[0], /权限被拒绝/);
});

/** 设备热拔出和 SDK 重建均有可清理监听器，保证真实状态不会冻结为最后一次按钮结果。 */
test('native track end and SDK replacement refresh status and release listeners', () => {
  const track = createTrack();
  const room = createRoom(track);
  const controls = createControls({ room });
  const previousMedia = track.mediaStreamTrack;
  assert.equal(previousMedia.listenerCount('ended'), 1);
  track.mediaStreamTrack.readyState = 'ended';
  track.mediaStreamTrack.dispatchEvent(new Event('ended'));
  controls.render();
  assert.equal(controls.element('button', '开启麦克风').props['aria-pressed'], false);
  track.mediaStreamTrack = createMediaTrack();
  track.emit('restarted');
  controls.render();
  assert.equal(controls.element('button', '关闭麦克风').props['aria-pressed'], true);
  assert.equal(previousMedia.listenerCount('ended'), 0);
  assert.equal(track.mediaStreamTrack.listenerCount('ended'), 1);
  assert.equal(track.listenerCount('restarted'), 1);
  controls.unmount();
  assert.equal(track.listenerCount('restarted'), 0);
  assert.equal(track.mediaStreamTrack.listenerCount('ended'), 0);
  track.mediaStreamTrack.dispatchEvent(new Event('ended'));
  assert.equal(controls.writesAfterUnmount, 0);
});
