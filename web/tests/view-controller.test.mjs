import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { conversationPorts, delivery } from './helpers/conversation-ports.mjs';
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

/** 显式回到最新才恢复自动跟随，流式到达不会擅自结束用户的历史阅读。 */
test('回到最新入口恢复跟随并收起提示', async () => {
  const page = await view();
  const scroll = page.getByRole('log');
  Object.defineProperties(scroll, {
    scrollHeight: { value: 1200, configurable: true },
    clientHeight: { value: 300, configurable: true },
  });
  scroll.scrollTop = 100;
  fireEvent.scroll(scroll);
  page.append('new');
  assert.equal(scroll.scrollTop, 100);
  fireEvent.click(page.getByRole('button', { name: '回到最新' }));
  assert.equal(scroll.scrollTop, 1200);
  assert.equal(page.queryByRole('button', { name: '回到最新' }), null);
  page.append('newer');
  assert.equal(scroll.scrollTop, 1200);
});

/** 开合仅隐藏原字幕节点；结束后重新连接仍使用页面持有的草稿。 */
test('桌面浮层收起不丢失记录节点，结束重开保留未发送草稿', async () => {
  const page = await view();
  page.append('first');
  const history = page.container.querySelector('#chat-history');
  const scroll = page.getByRole('log');
  fireEvent.change(page.getByRole('textbox'), { target: { value: '还没发的文字' } });
  fireEvent.click(history.querySelector('.history-close'));
  assert.equal(history.getAttribute('aria-hidden'), 'true');
  const reopen = page.container.querySelector('.header-actions button[aria-label="查看聊天记录"]');
  fireEvent.click(reopen);
  assert.equal(page.getByRole('log'), scroll);
  assert.equal(history.getAttribute('aria-hidden'), 'false');
  page.update({ phase: 'ending' });
  page.update({ phase: 'idle' });
  page.update({ phase: 'active', conversationId: 2 });
  assert.equal(page.getByRole('textbox').value, '还没发的文字');
  assert.match(scroll.textContent, /固定消息 first/);
});

/** 手机历史提供真正的模态焦点边界；Escape 后归还入口，不重建输入和会话。 */
test('手机历史抽屉默认关闭，循环焦点并支持 Escape 归还入口', async (t) => {
  const breakpoint = Object.assign(new window.EventTarget(), { matches: true });
  t.mock.method(window, 'matchMedia', () => breakpoint);
  const page = await view();
  assert.equal(page.queryByRole('dialog'), null);
  const opener = page.getByRole('button', { name: '查看聊天记录' });
  opener.focus();
  fireEvent.click(opener);
  const dialog = page.getByRole('dialog', { name: '聊天记录抽屉' });
  const close = dialog.querySelector('.history-close');
  const log = page.getByRole('log');
  assert.equal(document.activeElement, close);
  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  assert.equal(page.container.querySelector('.dock-region').hasAttribute('inert'), true);
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
  assert.equal(document.activeElement, log);
  fireEvent.keyDown(document, { key: 'Tab' });
  assert.equal(document.activeElement, close);
  fireEvent.keyDown(document, { key: 'Escape' });
  assert.equal(page.queryByRole('dialog'), null);
  assert.equal(document.activeElement, opener);
  assert.equal(page.container.querySelector('.dock-region').hasAttribute('inert'), false);
});

/** 手机字幕跟随当前语义片段，完整回复仍在历史合并；长回复不能遮住正在说的新句。 */
test('手机只显示当前字幕片段，历史保留同回复完整正文', async (t) => {
  const breakpoint = Object.assign(new window.EventTarget(), { matches: true });
  t.mock.method(window, 'matchMedia', () => breakpoint);
  const page = await view();
  const first = '第一句完整回复。';
  const latest = '这是正在说的第二句。';
  page.update({
    messages: [first, latest].map((message, index) => ({
      id: String(index),
      timestamp: 0,
      from: { identity: 'xiaoya', isLocal: false },
      message,
      attributes: {
        'xiaoya.delivery': JSON.stringify(
          delivery(index + 1, {
            state: 'active',
            reply_id: 'reply-current',
            segment_id: String(index),
          })
        ),
      },
    })),
  });
  assert.equal(page.container.querySelector('.current-caption').textContent, latest);
  fireEvent.click(page.getByRole('button', { name: '查看聊天记录' }));
  assert.match(page.getByRole('log').textContent, new RegExp(first + latest));
});
