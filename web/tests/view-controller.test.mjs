import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { conversationPorts } from './helpers/conversation-ports.mjs';
import {
  React,
  cleanup,
  fireEvent,
  observers,
  production,
  render,
  settle,
} from './helpers/dom.mjs';

afterEach(cleanup);

/** 真实视图与控件保留 DOM 几何和 React 状态，只有模型端口省略以专注滚动与键盘布局。 */
async function view() {
  const environment = conversationPorts();
  const room = new environment.ports['livekit-client'].Room({});
  await room.start();
  const { ViewController } = await production('components/app/view-controller.tsx', {
    ...environment.ports,
    '@/components/app/avatar-stage': {
      AvatarStage: () => React.createElement('section', { 'aria-label': '小芽数字人' }),
    },
  });
  let conversation = {
    phase: 'active',
    error: '',
    microphoneFailed: false,
    microphoneCapturing: false,
    conversationId: 1,
    attempt: {
      room,
      audio: null,
      /** 几何用例不模拟连接投影，麦克风提示与迟到结果由真实 App 用例覆盖。 */
      reportMicrophone() {},
    },
    peer: { state: 'listening', agent: environment.agent },
    messages: [],
    sending: false,
    controller: { delivery: { current: new DeliveryGate() } },
    send: async () => ({}),
    /** 页面用例不分配真实媒体；连接生命周期由控制器用例单独验证。 */
    start() {},
    end: async () => undefined,
  };
  const rendered = render(React.createElement(ViewController, { conversation }));
  return {
    ...rendered,
    /** props 改变发生在原组件位置，页面内存不因测试手动重建而丢失。 */
    update(change) {
      conversation = { ...conversation, ...change };
      rendered.rerender(React.createElement(ViewController, { conversation }));
    },
    /** 字幕到达使用真实生产视图的 effect，测试不直接修改滚动跟随标记。 */
    append(id) {
      conversation = {
        ...conversation,
        messages: [...conversation.messages, { id, timestamp: 0, message: '固定消息 ' + id }],
      };
      rendered.rerender(React.createElement(ViewController, { conversation }));
    },
  };
}

/** 新会话才重置阅读意图；同房恢复、草稿变高和可见高度变化不得抢回历史滚动。 */
test('历史滚动在恢复与草稿重排期间保留，新会话重新跟随最新', async () => {
  const page = await view();
  const scroll = page.getByRole('log');
  Object.defineProperties(scroll, {
    scrollHeight: { value: 1000, configurable: true },
    clientHeight: { value: 300, configurable: true },
  });
  scroll.scrollTop = 0;
  fireEvent.scroll(scroll);
  page.update({ phase: 'recovering' });
  page.append('a');
  assert.equal(page.getByRole('textbox').disabled, true);
  assert.equal(scroll.scrollTop, 0);
  page.update({ phase: 'active' });
  fireEvent.change(page.getByRole('textbox'), { target: { value: '这是一份更长的草稿' } });
  await settle(() => {
    for (const observer of observers) observer.callback();
  });
  assert.equal(scroll.scrollTop, 0);
  page.update({ conversationId: 2 });
  page.append('b');
  assert.equal(scroll.scrollTop, 1000);
});

/** 页面高度来自真实浏览器事件，软键盘出现时收起装饰但保持输入与挂断入口可访问。 */
test('窄可见高度启用紧凑布局，卸载解除观察器', async () => {
  const page = await view();
  Object.defineProperty(window, 'innerHeight', { value: 440, configurable: true });
  await settle(() => window.dispatchEvent(new Event('resize')));
  const main = page.container.querySelector('main');
  assert.equal(main.dataset.compactHeight, 'true');
  assert.equal(main.style.getPropertyValue('--app-height'), '440px');
  assert.ok(page.getByRole('textbox'));
  assert.ok(page.getByRole('button', { name: '结束聊天' }));
  page.unmount();
  assert.equal(observers.size, 0);
});
