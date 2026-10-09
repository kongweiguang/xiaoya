export type AvatarBehavior = 'idle' | 'listening' | 'thinking' | 'speaking' | 'confused';
type AvatarState = {
  connected: boolean;
  reconnecting: boolean;
  error: boolean;
  agent: string;
};

/** 连接和错误优先于 SDK 的迟到 speaking；嘴型另由真实声音控制，状态只选神态。 */
export function selectBehavior(state: AvatarState): AvatarBehavior {
  if (state.error || state.reconnecting) return 'confused';
  if (!state.connected) return 'idle';
  if (state.agent === 'thinking') return 'thinking';
  if (state.agent === 'speaking') return 'speaking';
  return 'listening';
}

/** 累计截止点保留刷新间隔余数，144 Hz 也能均匀达到 60 fps，暂停后直接跳过旧帧。 */
export function advanceFrameDeadline(deadline: number, time: number, interval: number): number {
  return deadline + (Math.floor(Math.max(0, time - deadline) / interval) + 1) * interval;
}
