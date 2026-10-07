'use client';

import { useSyncExternalStore } from 'react';

/** 操作系统在通话中改变偏好也立即生效，不依赖组件重新挂载。 */
function subscribe(listener: () => void) {
  const query = window.matchMedia('(prefers-reduced-motion: reduce)');
  query.addEventListener('change', listener);
  return () => query.removeEventListener('change', listener);
}

/** 服务端保持静态默认值，水合后读取浏览器当前偏好。 */
function snapshot() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** 动画只影响画面，关闭动画时保持原有语音及文字操作。 */
export function useReducedMotionPreference() {
  return useSyncExternalStore(subscribe, snapshot, () => false);
}
