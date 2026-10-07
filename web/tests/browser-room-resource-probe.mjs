/** 仅在独立验收标签的预加载脚本中使用，统计真实音频时钟而不持有原生资源引用。 */
export const installSource = String.raw`(() => {
  const Original = window.AudioContext;
  const records = [];
  /** 使用原生构造与原生关闭，探针只保存状态快照，不改变播放图或会话所有权。 */
  class ObservedAudioContext extends Original {
    /** 构造统计限于这个标签，状态监听关闭后自清理，不通过强引用保存上下文。 */
    constructor(...arguments_) {
      super(...arguments_);
      const record = { id: records.length + 1, state: this.state, createdAt: performance.now(), closedAt: null };
      records.push(record);
      /** 关闭后立即解除监听，记录不保存 context，避免验收探针自己制造资源残留。 */
      const changed = () => {
        record.state = this.state;
        if (this.state === 'closed') {
          record.closedAt = performance.now();
          this.removeEventListener('statechange', changed);
        }
      };
      this.addEventListener('statechange', changed);
    }
  }
  window.AudioContext = ObservedAudioContext;
  /** 返回复制后的计数，读取不能修改累积记录，且不暴露任何音频数据或令牌。 */
  window.__roomAudioProbe = () => ({
    created: records.length,
    closed: records.filter((record) => record.state === 'closed').length,
    live: records.filter((record) => record.state !== 'closed').length,
    records: records.map((record) => ({ ...record })),
  });
})();`;

/** 快照只读取可见状态和 DOM 媒体数量，不调用私有 React 或 LiveKit 实例。 */
export const snapshotSource = String.raw`(() => ({
  at: new Date().toISOString(),
  buttons: Array.from(document.querySelectorAll('button')).map((button) => ({ text: button.textContent.trim(), disabled: button.disabled })),
  status: Array.from(document.querySelectorAll('[role="status"], [aria-live]')).map((element) => element.textContent.trim()),
  lipSync: Array.from(document.querySelectorAll('[data-lip-sync]')).map((element) => element.getAttribute('data-lip-sync')),
  audioElements: Array.from(document.querySelectorAll('audio')).map((audio) => ({ paused: audio.paused, ended: audio.ended, readyState: audio.readyState, hasSourceObject: Boolean(audio.srcObject) })),
  contexts: window.__roomAudioProbe?.() ?? null,
  text: document.body.innerText.slice(0, 2400),
}))()`;
