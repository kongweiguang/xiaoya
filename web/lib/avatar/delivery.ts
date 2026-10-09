import type { AvatarDelivery } from './presentation';

type DeliveryState = {
  v: 1;
  instance: string;
  revision: number;
  reply_id: string;
  segment_id: string;
  state: 'active' | 'closed';
  style: AvatarDelivery['style'];
  gesture: AvatarDelivery['gesture'];
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
      data?.v !== 1 ||
      typeof data.instance !== 'string' ||
      !data.instance ||
      !Number.isSafeInteger(data.revision) ||
      data.revision < 0 ||
      typeof data.reply_id !== 'string' ||
      typeof data.segment_id !== 'string' ||
      !['active', 'closed'].includes(data.state) ||
      !styles.has(data.style) ||
      !gestures.has(data.gesture) ||
      (data.state === 'active' && (!data.reply_id || !data.segment_id))
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
  private synchronized = false;
  private presentationInvalidated = false;
  private recoveryFence: {
    instance: string;
    revision: number;
    reply: string | null;
  } | null = null;

  /** 连接代次仍独占回调，但保留旧回复取消边界；不同 Agent 实例必须由新快照解除。 */
  constructor(previous?: DeliveryGate) {
    this.presentationInvalidated = previous?.presentationInvalidated ?? false;
    const prior = previous?.current ?? previous?.pending;
    this.recoveryFence = prior
      ? {
          instance: prior.instance,
          revision: Math.max(prior.revision, previous!.consumedRevision),
          reply: prior.state === 'active' ? prior.reply_id : previous!.blockedReply,
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
      this.consumedRevision = fence.revision;
      // 旧快照也不能回退已知序号；只恢复基线，等待权威的新回复而不补演旧段。
      if (next.revision < fence.revision)
        next = {
          ...next,
          revision: fence.revision,
          state: 'closed',
          style: 'neutral',
          gesture: 'none',
        };
    }
    this.apply(next);
    if (this.presentationInvalidated) {
      this.presentationInvalidated = false;
      this.interrupt();
    }
  }

  /** active 必须匹配字幕头；replyKey 显式携带实例/回复而不解析段 ID，JSON 二元数组避免拼接歧义。 */
  offer(messages: readonly DeliveryMessage[], agentIdentity: string) {
    const state = this.current;
    if (
      !this.synchronized ||
      !state ||
      state.state !== 'active' ||
      state.revision <= this.consumedRevision
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
        header.revision === state.revision &&
        header.reply_id === state.reply_id &&
        header.segment_id === state.segment_id &&
        message.attributes?.['lk.segment_id'] === state.segment_id
      ) {
        this.candidate = {
          id: `${state.instance}:${state.revision}:${state.segment_id}`,
          replyKey: JSON.stringify([state.instance, state.reply_id]),
          style: state.style,
          gesture: state.gesture,
        };
        return;
      }
    }
  }

  /** 用户开口、换轨或播放受阻立即关闭表现，不因下一次 speaking 自行补演旧动作。 */
  interrupt() {
    if (this.current?.state === 'active') {
      this.blockedReply = this.current.reply_id;
      this.consumedRevision = Math.max(this.consumedRevision, this.current.revision);
    }
    this.candidate = null;
    this.displayed = null;
  }

  /** 真实播放能力失效才延续取消到首份快照；普通初始化不能消耗尚未开始的开场白。 */
  invalidatePresentation() {
    if (!this.synchronized) this.presentationInvalidated = true;
    this.interrupt();
  }

  /** 首次表现等待真实音频；句内短暂静音保留神态，手势不会因静音重新触发。 */
  read(playbackAvailable: boolean, audible: boolean): AvatarDelivery | null {
    if (!playbackAvailable || this.current?.state !== 'active') return null;
    if (this.blockedReply === this.current.reply_id) return null;
    if (this.candidate && audible) {
      this.displayed = this.candidate;
      this.candidate = null;
      this.consumedRevision = this.current.revision;
    }
    return this.displayed;
  }

  /** 一个状态更新唯一改变当前段；结束和新序号均清除旧姿态的所有权。 */
  private apply(state: DeliveryState) {
    this.current = state;
    this.candidate = null;
    this.displayed = null;
    // 更高序号的新回复已经证明旧回复失去所有权，不依赖某个关闭通知一定送达。
    if (state.state === 'active' && this.blockedReply && state.reply_id !== this.blockedReply) {
      this.blockedReply = null;
    }
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
