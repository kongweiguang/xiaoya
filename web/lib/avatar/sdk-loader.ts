const scripts = new Map<string, Promise<void>>();

/** 同源固定版本只加载一次；失败移除缓存，让明确重试能重新请求而不会叠加全局 SDK。 */
export function loadSdkScript(src: string): Promise<void> {
  const cached = scripts.get(src);
  if (cached) return cached;
  const pending = new Promise<void>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    const timer = setTimeout(() => finish(new Error('人物引擎加载超时')), 15_000);
    /** 成功和失败都解除事件及计时器，避免晚到事件覆盖已经结束的加载。 */
    function finish(error?: Error) {
      clearTimeout(timer);
      script.onload = null;
      script.onerror = null;
      if (error) {
        script.remove();
        scripts.delete(src);
        reject(error);
      } else resolve();
    }
    script.onload = () => finish();
    script.onerror = () => finish(new Error('人物引擎暂不可用'));
    document.head.append(script);
  });
  scripts.set(src, pending);
  return pending;
}
