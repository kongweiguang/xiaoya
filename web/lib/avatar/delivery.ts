import type { AvatarDelivery } from './presentation';

export type DeliveryState = {
  v: 2;
  instance: string;
  revision: number;
  reply_id: string;
  segment_id: string;
  state: 'active' | 'closed';
  style: AvatarDelivery['style'];
  gesture: AvatarDelivery['gesture'];
  phase: AvatarDelivery['phase'];
  turn_id: string;
  gesture_id: string;
  energy: number;
};
export type DeliveryMessage = {
  id: string;
  message: string;
  attributes?: Record<string, string>;
  from?: { identity: string; isLocal: boolean };
};
const styles = new Set(['neutral', 'happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised']);
const gestures = new Set(['none', 'nod', 'tilt', 'wave', 'shy', 'shake']);

/** 小型版本化消息只描述受控表现，损坏或未知协议保持中性而不影响字幕。 */
export function parseDelivery(value: string): DeliveryState | null {
  if (value.length > 4096) return null;
  try {
    const data = JSON.parse(value);
    if (
      data?.v !== 2 ||
      typeof data.instance !== 'string' ||
      !data.instance ||
      !Number.isSafeInteger(data.revision) ||
      data.revision < 0 ||
      typeof data.reply_id !== 'string' ||
      typeof data.segment_id !== 'string' ||
      !['active', 'closed'].includes(data.state) ||
      !styles.has(data.style) ||
      !gestures.has(data.gesture) ||
      !['idle', 'listening', 'thinking', 'speaking'].includes(data.phase) ||
      (data.phase === 'idle' && data.state !== 'closed') ||
      (data.phase !== 'idle' && data.state !== 'active') ||
      typeof data.turn_id !== 'string' ||
      typeof data.gesture_id !== 'string' ||
      typeof data.energy !== 'number' ||
      !Number.isFinite(data.energy) ||
      data.energy < 0 ||
      data.energy > 1 ||
      (data.state === 'active' &&
        data.phase === 'speaking' &&
        (!data.reply_id ||
          !data.segment_id ||
          !data.turn_id ||
          (data.gesture !== 'none' && !data.gesture_id))) ||
      (data.phase !== 'speaking' && (data.gesture !== 'none' || data.gesture_id !== '')) ||
      (data.state === 'active' && data.phase !== 'idle' && !data.turn_id)
    )
      return null;
    return data as DeliveryState;
  } catch {
    return null;
  }
}

export class DeliveryGate {
  private current: DeliveryState | null = null;
  private pending: DeliveryState | null = null;
  private candidate: AvatarDelivery | null = null;
  private displayed: AvatarDelivery | null = null;
  private consumedRevision = -1;
  private blockedReply: string | null = null;
  private readonly blockedReplies = new Set<string>();
  private readonly blockedTurns = new Set<string>();
  private synchronized = false;
  private presentationInvalidated = false;
  private playbackInvalidated = false;
  private recoveryFence: {
    instance: string;
    revision: number;
    reply: string | null;
    turn: string | null;
  } | null = null;

  /** 连接代次仍独占回调，但保留旧回复取消边界；不同 Agent 实例必须由新快照解除。 */
  constructor(previous?: DeliveryGate) {
    this.presentationInvalidated = previous?.presentationInvalidated ?? false;
    this.playbackInvalidated = previous?.playbackInvalidated ?? false;
    if (previous) {
      for (const reply of previous.blockedReplies) this.blockedReplies.add(reply);
      for (const turn of previous.blockedTurns) this.blockedTurns.add(turn);
    }
    const prior = previous?.current ?? previous?.pending;
    this.recoveryFence = prior
      ? {
          instance: prior.instance,
          revision: Math.max(prior.revision, previous!.consumedRevision),
          reply: prior.state === 'active' ? prior.reply_id : previous!.blockedReply,
          turn: prior.state === 'active' && prior.phase !== 'speaking' ? prior.turn_id : null,
        }
      : (previous?.recoveryFence ?? null);
  }

  /** 只读开发诊断只含协议身份与门控状态，不暴露文本、凭据或音频。 */
  get diagnostics() {
    return {
      state: this.current,
      synchronized: this.synchronized,
      candidate: this.candidate,
      displayed: this.displayed,
      blockedReply: this.blockedReply,
      consumedRevision: this.consumedRevision,
    };
  }

  /** 已匹配字幕的候选可等待设备输出缓冲；舞台不能把这种合法未来 PCM 当取消尾窗清掉。 */
  get awaitingAudibleSpeech(): boolean {
    const state = this.current;
    return Boolean(
      this.synchronized &&
        state?.state === 'active' &&
        state.phase === 'speaking' &&
        this.candidate &&
        !this.blockedReplies.has(state.reply_id) &&
        !this.blockedTurns.has(state.turn_id)
    );
  }

  /** 数据订阅先于快照建立，但实例身份只由此次 RPC 快照确认。 */
  receive(state: DeliveryState) {
    if (!this.synchronized) {
      if (!this.pending || state.revision > this.pending.revision) this.pending = state;
      return;
    }
    if (state.instance !== this.current?.instance || state.revision <= this.current.revision)
      return;
    this.apply(state);
  }

  /** 快照先合并订阅状态；同步前真实失效过的表现只建基线，不让迟到回复补演。 */
  snapshot(state: DeliveryState) {
    if (this.synchronized) return;
    this.synchronized = true;
    let next =
      this.pending?.instance === state.instance && this.pending.revision > state.revision
        ? this.pending
        : state;
    this.pending = null;
    const fence = this.recoveryFence;
    this.recoveryFence = null;
    if (fence?.instance === next.instance) {
      this.blockedReply = fence.reply;
      if (fence.reply) this.blockedReplies.add(fence.reply);
      if (fence.turn) this.blockedTurns.add(fence.turn);
      this.consumedRevision = fence.revision;
      // 旧快照也不能回退已知序号；只恢复基线，等待权威的新回复而不补演旧段。
      if (next.revision < fence.revision)
        next = {
          ...next,
          revision: fence.revision,
          state: 'closed',
          phase: 'idle',
          style: 'neutral',
          gesture: 'none',
          gesture_id: '',
        };
    } else if (fence) {
      this.blockedReplies.clear();
      this.blockedTurns.clear();
      this.blockedReply = null;
    }
    this.apply(next);
    if (this.presentationInvalidated) {
      this.presentationInvalidated = false;
      this.interrupt();
    }
    if (this.playbackInvalidated) {
      this.playbackInvalidated = false;
      this.interruptSpeaking();
    }
  }

  /** 字幕只确认不可变片段身份；视觉修订号可独立前进，Jev 更新不要求重发字幕。 */
  offer(messages: readonly DeliveryMessage[], agentIdentity: string) {
    const state = this.current;
    if (
      !this.synchronized ||
      !state ||
      state.state !== 'active' ||
      state.phase !== 'speaking' ||
      this.blockedReplies.has(state.reply_id) ||
      this.blockedTurns.has(state.turn_id)
    )
      return;
    for (let index = messages.length - 1; index >= 0; index--) {
      const message = messages[index];
      if (message.from?.identity !== agentIdentity || !message.message.trim()) continue;
      const raw = message.attributes?.['xiaoya.delivery'];
      const header = raw ? parseDelivery(raw) : null;
      if (
        header &&
        header.instance === state.instance &&
        header.phase === 'speaking' &&
        header.reply_id === state.reply_id &&
        header.segment_id === state.segment_id &&
        message.attributes?.['lk.segment_id'] === state.segment_id
      ) {
        const candidate = this.delivery(state);
        if (this.displayed?.id !== candidate.id) this.candidate = candidate;
        return;
      }
    }
  }

  /** 舞台隐藏和连接换代封存当前轮次；未来更高修订也不能复活错过的同轮动作。 */
  interrupt() {
    if (this.current?.state === 'active') {
      if (this.current.phase !== 'speaking' && this.current.turn_id)
        this.blockedTurns.add(this.current.turn_id);
    }
    this.interruptSpeaking();
    this.candidate = null;
    this.displayed = null;
  }

  /** 用户开口或播放许可丢失只截断旧说话，仍允许无音频依赖的当前倾听表情。 */
  interruptSpeaking() {
    if (this.current?.state === 'active' && this.current.phase === 'speaking') {
      this.blockedReply = this.current.reply_id;
      this.blockedReplies.add(this.current.reply_id);
      this.consumedRevision = Math.max(this.consumedRevision, this.current.revision);
      this.candidate = null;
      this.displayed = null;
    }
  }

  /** 真实播放能力失效才延续取消到首份快照；普通初始化不能消耗尚未开始的开场白。 */
  invalidatePresentation() {
    if (!this.synchronized) this.presentationInvalidated = true;
    this.interrupt();
  }

  /** 音频初始化和浏览器播放受阻不会取消倾听；同步前失效只延续到首份说话快照。 */
  invalidatePlayback() {
    if (!this.synchronized) this.playbackInvalidated = true;
    this.interruptSpeaking();
  }

  /** 倾听不依赖声音；说话必须先有匹配字幕与实际出声，句内静音保持已展示神态。 */
  read(
    playbackAvailable: boolean,
    audible: boolean,
    visualAvailable = true
  ): AvatarDelivery | null {
    const state = this.current;
    if (!visualAvailable || !this.synchronized || state?.state !== 'active') return null;
    if (this.blockedTurns.has(state.turn_id)) return null;
    if (state.phase !== 'speaking') return this.delivery(state);
    if (!playbackAvailable || this.blockedReplies.has(state.reply_id)) return null;
    const sameSegment =
      this.displayed?.segmentKey ===
      JSON.stringify([state.instance, state.reply_id, state.segment_id]);
    if (this.candidate && (audible || sameSegment)) {
      this.displayed = this.candidate;
      this.candidate = null;
      this.consumedRevision = state.revision;
    }
    return this.displayed;
  }

  /** 同片段视觉增强保留播放许可，片段或阶段变更必须重新证明当前所有权。 */
  private apply(state: DeliveryState) {
    const previous = this.current;
    const sameSegment =
      previous?.state === 'active' &&
      state.state === 'active' &&
      previous.phase === 'speaking' &&
      state.phase === 'speaking' &&
      previous.reply_id === state.reply_id &&
      previous.segment_id === state.segment_id;
    this.current = state;
    this.candidate = null;
    if (!sameSegment) this.displayed = null;
    // 更高序号的新回复已经证明旧回复失去所有权，不依赖某个关闭通知一定送达。
    if (state.state === 'active' && this.blockedReply && state.reply_id !== this.blockedReply) {
      this.blockedReply = null;
    }
  }

  /** 稳定动作身份与可变视觉版本分开，重复字幕或表情变化不会重新启动已消费手势。 */
  private delivery(state: DeliveryState): AvatarDelivery {
    return {
      id: `${state.instance}:${state.revision}:${state.segment_id}`,
      replyKey: JSON.stringify([state.instance, state.reply_id || state.turn_id]),
      segmentKey: JSON.stringify([state.instance, state.reply_id, state.segment_id]),
      phase: state.phase,
      turnId: state.turn_id,
      gestureId: state.gesture_id,
      energy: state.energy,
      style: state.style,
      gesture: state.gesture,
    };
  }
}

export type SubtitleMessage = DeliveryMessage & { timestamp: number };

/** 同一次回复的相邻句段只在显示层合并，原始字幕仍保留句段身份供表现协议核对。 */
export function groupReplyMessages(messages: readonly SubtitleMessage[]): SubtitleMessage[] {
  const grouped: SubtitleMessage[] = [];
  for (const message of messages) {
    const previous = grouped.at(-1);
    const raw = message.attributes?.['xiaoya.delivery'];
    const previousRaw = previous?.attributes?.['xiaoya.delivery'];
    const state = raw ? parseDelivery(raw) : null;
    const previousState = previousRaw ? parseDelivery(previousRaw) : null;
    if (
      !message.from?.isLocal &&
      previous &&
      message.from?.identity === previous.from?.identity &&
      state &&
      previousState &&
      state.instance === previousState.instance &&
      state.reply_id === previousState.reply_id
    ) {
      grouped[grouped.length - 1] = { ...previous, message: previous.message + message.message };
    } else grouped.push({ ...message });
  }
  return grouped;
}
