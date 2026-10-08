import React from 'react';
import { JSDOM } from 'jsdom';

export { production } from './production.mjs';

const browser = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://127.0.0.1:3000/',
  pretendToBeVisual: true,
});
for (const name of [
  'window',
  'document',
  'navigator',
  'HTMLElement',
  'HTMLCanvasElement',
  'MutationObserver',
  'Event',
  'EventTarget',
  'KeyboardEvent',
  'MouseEvent',
  'Node',
  'getComputedStyle',
])
  Object.defineProperty(globalThis, name, {
    value: name === 'window' ? browser.window : browser.window[name],
    configurable: true,
  });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const media = Object.assign(new window.EventTarget(), {
  matches: false,
  /** 生产订阅使用公开 matchMedia 事件，测试不会直接调用组件 effect。 */
  addListener(listener) {
    this.addEventListener('change', listener);
  },
  /** 模拟公开旧浏览器端口只为框架依赖，业务本身使用标准 change 监听。 */
  removeListener(listener) {
    this.removeEventListener('change', listener);
  },
});
window.matchMedia = () => media;
export const observers = new Set();
globalThis.ResizeObserver = class {
  /** DOM 几何由测试显式设置，观察器只保存真实组件提交的回调。 */
  constructor(callback) {
    this.callback = callback;
  }
  /** jsdom 没有布局引擎，不能通过虚构尺寸事件替组件恢复滚动。 */
  observe() {
    observers.add(this);
  }
  /** 测试退出仍经过生产 cleanup，只是不持有浏览器原生句柄。 */
  disconnect() {
    observers.delete(this);
  }
};
export const { act, render, renderHook, fireEvent, cleanup, waitFor, screen } = await import(
  '@testing-library/react'
);
export { React, media };

/** Promise 完成由测试控制，复现迟到权限、发布和 RPC 而不依赖机器速度。 */
export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

/** React 提交与微任务在同一 act 内排空，避免用睡眠掩盖真实生命周期竞态。 */
export async function settle(action = () => undefined) {
  await act(async () => {
    await action();
    for (let i = 0; i < 12; i++) await Promise.resolve();
  });
}
