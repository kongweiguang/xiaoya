import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { LocalTrack, conversationPorts } from './helpers/conversation-ports.mjs';
import {
  React,
  cleanup,
  deferred,
  fireEvent,
  observers,
  production,
  render,
  settle,
} from './helpers/dom.mjs';

afterEach(cleanup);

/** 控件运行真实 React DOM，浏览器设备与 SDK publication 是唯一受控端口。 */
async function controls(options = {}) {
  const environment = conversationPorts({ microphone: options.acquire });
  const room = new environment.ports['livekit-client'].Room({});
  room.state = 'connected';
  room.deviceId = 'device-a';
  if (options.track)
    room.localParticipant.audioTrackPublications.set('microphone', {
      track: options.track,
      audioTrack: options.track,
    });
  room.notify();
  options.configure?.(room);
  const { ConversationControls } = await production(
    'components/app/conversation-controls.tsx',
    environment.ports
  );
  /** 草稿必须由真实父组件持有，迟到发送完成不能直接改 DOM 跳过 React 更新器。 */
  function Composer() {
    const [draft, setDraft] = React.useState(options.draft ?? '');
    return React.createElement(ConversationControls, {
      room,
      draft,
      setDraft,
      ready: options.ready ?? true,
      sending: false,
      send: options.send ?? (async () => ({})),
      end: async () => undefined,
      /** 独立控件用例只观察采集端口，全页面提示同步由真实 App 用例验证。 */
      onMicrophoneChange: options.onMicrophoneChange ?? (() => undefined),
    });
  }
  return { ...environment, room, ...render(React.createElement(Composer)) };
}

/** 失败切换可能结束旧轨，恢复选择与采集必须在相同公开按钮路径完成。 */
test('设备切换失败清理失效 publication，并能一键重新开启', async () => {
  const previous = new LocalTrack();
  const recovered = new LocalTrack();
  const view = await controls({
    track: previous,
    acquire: async () => recovered,
    /** 注入设备层拒绝，不替换真实控件的错误恢复流程。 */
    configure(room) {
      room.switchFailure = new DOMException('private', 'NotReadableError');
    },
  });
  await settle(() =>
    fireEvent.change(view.getByRole('combobox'), { target: { value: 'device-b' } })
  );
  assert.deepEqual(view.room.switched, ['device-b', 'device-a']);
  assert.deepEqual(view.room.unpublish, [previous]);
  assert.equal(view.getByRole('combobox').value, 'device-a');
  assert.match(view.getByRole('alert').textContent, /麦克风暂不可用/);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.equal(view.calls.microphones[0].deviceId, 'device-a');
  assert.deepEqual(view.room.published, [recovered]);
  assert.equal(
    view.getByRole('button', { name: '关闭麦克风' }).getAttribute('aria-pressed'),
    'true'
  );
  assert.equal(view.queryByRole('alert'), null);
});

/** 已 ended 但未 muted 的轨道不能被显示成可用采集，也不应需要用户先关再开。 */
test('失效但未静音的轨道直接重新采集', async () => {
  const previous = new LocalTrack();
  previous.mediaStreamTrack.readyState = 'ended';
  const view = await controls({ track: previous });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.deepEqual(view.room.unpublish, [previous]);
  assert.equal(view.room.published.length, 1);
  assert.ok(view.getByRole('button', { name: '关闭麦克风' }));
});

/** 选择、权限和开关共享互斥操作，快速重复事件不能覆盖失败后的恢复选择。 */
test('设备操作串行，等待期间禁用选择与开关', async () => {
  const pending = deferred();
  const view = await controls({
    track: new LocalTrack(),
    /** 保持操作未完成，才能验证重复点击受到同一互斥边界约束。 */
    configure(room) {
      room.switchGate = pending;
    },
  });
  fireEvent.change(view.getByRole('combobox'), { target: { value: 'device-b' } });
  fireEvent.change(view.getByRole('combobox'), { target: { value: 'device-a' } });
  fireEvent.click(view.getByRole('button', { name: '关闭麦克风' }));
  assert.equal(view.getByRole('combobox').disabled, true);
  assert.deepEqual(view.room.switched, ['device-b']);
  await settle(() => pending.resolve());
  assert.equal(view.getByRole('combobox').disabled, false);
});

/** SDK false 结果不算成功，错误与恢复入口要与拒绝 Promise 的路径一致。 */
test('SDK 未切换成功时恢复旧设备并展示错误', async () => {
  const view = await controls({
    /** SDK 可返回 false 而非抛错，端口保留这个独立失败契约。 */
    configure(room) {
      room.switchResult = false;
    },
  });
  await settle(() =>
    fireEvent.change(view.getByRole('combobox'), { target: { value: 'device-b' } })
  );
  assert.deepEqual(view.room.switched, ['device-b', 'device-a']);
  assert.equal(view.getByRole('combobox').value, 'device-a');
  assert.match(view.getByRole('alert').textContent, /麦克风暂不可用/);
});

/** 开启失败可能使原生轨道结束，错误之后的同一个按钮需要直接创建新采集。 */
test('重新开启失败也清理轨道，再次点击可恢复', async () => {
  const previous = new LocalTrack();
  previous.isMuted = true;
  const view = await controls({
    track: previous,
    /** 权限失败仅从外部设备端口触发，仍由真实按钮管理旧轨清理与重试。 */
    configure(room) {
      room.enableFailure = new DOMException('private', 'NotAllowedError');
    },
  });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.deepEqual(view.room.unpublish, [previous]);
  assert.match(view.getByRole('alert').textContent, /权限被拒绝/);
  view.room.enableFailure = undefined;
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.equal(view.room.published.length, 1);
  assert.ok(view.getByRole('button', { name: '关闭麦克风' }));
});

/** SDK 在 map 移除后协商失败仍已释放原 publication，不能阻止真实重试。 */
test('取消发布协商失败不阻止已经移除的轨道恢复', async () => {
  const previous = new LocalTrack();
  previous.mediaStreamTrack.readyState = 'ended';
  const view = await controls({
    track: previous,
    /** 模拟 map 已移除但信令协商失败，避免将 SDK 抛错等同于资源仍被持有。 */
    configure(room) {
      room.unpublishFailure = new Error('private');
    },
  });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.equal(view.room.published.length, 1);
  assert.equal(view.queryByRole('alert'), null);
});

/** 尚未从 map 移除时必须拒绝重复发布，保留重试按钮等待 SDK 恢复。 */
test('未移除的失效 publication 不重复采集或发布', async () => {
  const previous = new LocalTrack();
  previous.mediaStreamTrack.readyState = 'ended';
  const view = await controls({ track: previous });
  const original = view.room.localParticipant.unpublishTrack;
  view.room.localParticipant.unpublishTrack = async () => {
    throw new Error('not removed');
  };
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.equal(view.calls.microphones.length, 0);
  assert.equal(view.room.published.length, 0);
  view.room.localParticipant.unpublishTrack = original;
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.equal(view.room.published.length, 1);
});

/** 权限不能被浏览器撤销，卸载后的结果必须只释放自己而不发布或改界面。 */
test('卸载后迟到权限停止采集且不发布', async () => {
  const pending = deferred();
  const track = new LocalTrack();
  const view = await controls({ acquire: () => pending.promise });
  fireEvent.click(view.getByRole('button', { name: '开启麦克风' }));
  await settle();
  assert.equal(view.calls.microphones.length, 1);
  view.unmount();
  await settle(() => pending.resolve(track));
  assert.equal(track.stops, 1);
  assert.equal(view.room.published.length, 0);
});

/** 发布 Promise 可晚于 UI 卸载，第二个异步边界也需要取消 publication 与停止采集。 */
test('卸载后迟到发布撤回并停止原轨道', async () => {
  const pending = deferred();
  const track = new LocalTrack();
  const view = await controls({
    acquire: async () => track,
    /** 将迟到结果停在发布边界，避免只覆盖权限请求的取消。 */
    configure(room) {
      room.publishGate = pending;
    },
  });
  fireEvent.click(view.getByRole('button', { name: '开启麦克风' }));
  await settle();
  view.unmount();
  view.room.state = 'disconnected';
  await settle(() => pending.resolve());
  assert.deepEqual(view.room.unpublish, [track]);
  assert.equal(track.mediaStreamTrack.readyState, 'ended');
});

/** 设备失败如果迟到，不可重新打开已退出的会话或恢复旧设备选择。 */
test('卸载后的设备失败仅清理旧轨道', async () => {
  const pending = deferred();
  const previous = new LocalTrack();
  const view = await controls({
    track: previous,
    /** 故意让失败晚于卸载，旧异步结果不得再次接管控件状态。 */
    configure(room) {
      room.switchGate = pending;
      room.switchFailure = new DOMException('private', 'NotReadableError');
    },
  });
  fireEvent.change(view.getByRole('combobox'), { target: { value: 'device-b' } });
  view.unmount();
  view.room.state = 'disconnected';
  await settle(() => pending.resolve());
  assert.deepEqual(view.room.unpublish, [previous]);
  assert.deepEqual(view.room.switched, ['device-b']);
});

/** 设备故障不废弃文字能力；两个操作的错误仍各自保留，发送重试只清自己的草稿与提示。 */
test('发送失败保留草稿，设备错误不阻止文字重试', async () => {
  let fails = true;
  const sent = [];
  const view = await controls({
    draft: '你好，小芽',
    send: async (text) => {
      sent.push(text);
      if (fails) throw new Error('private');
    },
    acquire: async () => {
      throw new DOMException('private', 'NotAllowedError');
    },
  });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '发送文字' })));
  await settle(() => fireEvent.click(view.getByRole('button', { name: '开启麦克风' })));
  assert.equal(view.getAllByRole('alert').length, 2);
  assert.equal(view.getByRole('textbox').value, '你好，小芽');
  fails = false;
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试发送' })));
  assert.deepEqual(sent, ['你好，小芽', '你好，小芽']);
  assert.equal(view.getByRole('textbox').value, '');
  assert.match(view.getByRole('alert').textContent, /权限被拒绝/);
});

/** 原生 ended 与 SDK restarted 使用真实 DOM／EventEmitter 事件，组件必须重绑并释放监听。 */
test('热拔出和同包装对象换轨刷新采集状态，卸载解除监听', async () => {
  const track = new LocalTrack();
  const view = await controls({ track });
  await settle(() => {
    track.mediaStreamTrack.readyState = 'ended';
    track.mediaStreamTrack.dispatchEvent(new Event('ended'));
  });
  assert.ok(view.getByRole('button', { name: '开启麦克风' }));
  await settle(() => {
    track.mediaStreamTrack = new LocalTrack().mediaStreamTrack;
    track.emit('restarted');
  });
  assert.ok(view.getByRole('button', { name: '关闭麦克风' }));
  assert.equal(track.listenerCount('restarted'), 1);
  view.unmount();
  assert.equal(track.listenerCount('restarted'), 0);
});

/** 完成交付前仍可继续编辑，成功回调只能清除本次提交的内容。 */
test('迟到发送成功保留等待期间新写的草稿', async () => {
  const pending = deferred();
  const sent = [];
  const view = await controls({
    draft: '第一条',
    send: async (text) => {
      sent.push(text);
      await pending.promise;
    },
  });
  fireEvent.click(view.getByRole('button', { name: '发送文字' }));
  fireEvent.change(view.getByRole('textbox'), { target: { value: '后来写的' } });
  await settle(() => pending.resolve());
  assert.deepEqual(sent, ['第一条']);
  assert.equal(view.getByRole('textbox').value, '后来写的');
  assert.equal(document.activeElement, view.getByRole('textbox'));
});

/** 组合输入和 Shift Enter 均不能误发，只有用户完成输入后的 Enter 才交付文字。 */
test('中文输入法与多行输入不误发送，连续提交受互斥保护', async () => {
  const pending = deferred();
  const sent = [];
  const view = await controls({
    draft: '你好',
    send: async (text) => {
      sent.push(text);
      await pending.promise;
    },
  });
  const input = view.getByRole('textbox');
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: true });
  fireEvent.compositionEnd(input);
  fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
  assert.deepEqual(sent, []);
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.keyDown(input, { key: 'Enter' });
  assert.deepEqual(sent, ['你好']);
  await settle(() => pending.resolve());
  assert.equal(input.value, '');
});

/** 草稿不变的宽度变化仍需重新换行测量；高度通知不得回环，卸载须释放唯一观察器。 */
test('草稿随实际宽度变化扩高与收回，同宽度通知不重复测量且卸载断开', async (t) => {
  const draft = '这是缩到窄屏后仍完整可见的未发送草稿。';
  const sent = [];
  const view = await controls({
    draft,
    /** 尺寸通知不能作为发送动作，真实控件若误交付草稿会留下外部端口证据。 */
    send: async (text) => sent.push(text),
  });
  const input = view.getByRole('textbox');
  let width = 693;
  let contentHeight = 38;
  let reads = 0;
  let pendingFrame;
  /** 动画帧是浏览器端口；测试显式推进下一帧，不用睡眠替代真实 React 提交。 */
  t.mock.method(window, 'requestAnimationFrame', (callback) => {
    pendingFrame = callback;
    return 1;
  });
  /** 取消通过相同原生端口体现，卸载后不能只移除观察器却留下迟到测量。 */
  t.mock.method(window, 'cancelAnimationFrame', () => {
    pendingFrame = undefined;
  });
  Object.defineProperties(input, {
    clientWidth: {
      /** jsdom 无排版引擎，只控制浏览器尺寸端口，不替换生产测量或 React 副作用。 */
      get: () => width,
    },
    scrollHeight: {
      /** 测量计数用于证明高度反馈事件被忽略，不能只检查最终像素恰好相同。 */
      get: () => {
        reads += 1;
        return contentHeight;
      },
    },
  });
  assert.equal(observers.size, 1);
  const [observer] = observers;
  /** 尺寸通知与下一帧分开，回调仍为生产组件注册，测试不直接调用高度算法。 */
  async function resized() {
    await settle(() => {
      observer.callback([]);
      const update = pendingFrame;
      pendingFrame = undefined;
      update?.(0);
    });
  }
  await resized();
  assert.equal(input.style.height, '38px');
  width = 264;
  contentHeight = 58;
  await resized();
  assert.equal(input.style.height, '58px');
  assert.equal(input.value, draft);
  const resizedReads = reads;
  await resized();
  assert.equal(reads, resizedReads);
  width = 693;
  contentHeight = 38;
  await resized();
  assert.equal(input.style.height, '38px');
  assert.deepEqual(sent, []);
  width = 300;
  await settle(() => observer.callback([]));
  assert.equal(typeof pendingFrame, 'function');
  view.unmount();
  assert.equal(observers.size, 0);
  assert.equal(pendingFrame, undefined);
});

/** 内容编辑复用同一测量入口与观察器，长草稿保留滚动上限，删除后收回占用空间。 */
test('编辑草稿保留100px高度上限与唯一观察器，清空后回到单行', async () => {
  const view = await controls({ draft: '第一条草稿' });
  const input = view.getByRole('textbox');
  let contentHeight = 38;
  Object.defineProperties(input, {
    clientWidth: {
      /** 固定宽度隔离内容编辑路径，不让模拟宽度变化代替真实 React 更新。 */
      get: () => 264,
    },
    scrollHeight: {
      /** 高度来自浏览器端口，100px 限制和清空后的恢复仍由生产控件执行。 */
      get: () => contentHeight,
    },
  });
  assert.equal(observers.size, 1);
  const [observer] = observers;
  contentHeight = 180;
  await settle(() => fireEvent.change(input, { target: { value: '长草稿\n'.repeat(12) } }));
  assert.equal(input.style.height, '100px');
  assert.deepEqual([...observers], [observer]);
  contentHeight = 38;
  await settle(() => fireEvent.change(input, { target: { value: '' } }));
  assert.equal(input.style.height, '38px');
  assert.deepEqual([...observers], [observer]);
});
