export type AvatarBehavior = 'idle' | 'listening' | 'thinking' | 'speaking' | 'confused';
export type AvatarState = {
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

/** 时间相关过渡在相同刷新率下保持等效响应，后台恢复时限制步长以免动作跳变。 */
export function easeParameter(current: number, target: number, seconds: number, speed = 9): number {
  return current + (target - current) * (1 - Math.exp(-speed * Math.min(seconds, 0.05)));
}

/** 累计截止点保留刷新间隔余数，144 Hz 也能均匀达到 60 fps，暂停后直接跳过旧帧。 */
export function advanceFrameDeadline(deadline: number, time: number, interval: number): number {
  return deadline + (Math.floor(Math.max(0, time - deadline) / interval) + 1) * interval;
}
