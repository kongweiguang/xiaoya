import type { AvatarDelivery, AvatarGesture, PresentationAck } from './presentation';

export type DeliveryState = {
  v: 3;
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
  gesture_source: AvatarDelivery['gestureSource'];
  command_feedback: 'none' | 'retry' | 'clarify';
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
      data?.v !== 3 ||
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
      (data.state === 'closed' && data.phase !== 'idle') ||
      typeof data.turn_id !== 'string' ||
      typeof data.gesture_id !== 'string' ||
      !['none', 'dialogue', 'command'].includes(data.gesture_source) ||
      !['none', 'retry', 'clarify'].includes(data.command_feedback) ||
      typeof data.energy !== 'number' ||
      !Number.isFinite(data.energy) ||
      data.energy < 0 ||
      data.energy > 1 ||
      (data.state === 'active' &&
        data.phase === 'speaking' &&
        data.gesture_source !== 'command' &&
        (!data.reply_id ||
          !data.segment_id ||
          !data.turn_id ||
          (data.gesture !== 'none' && !data.gesture_id))) ||
      (data.gesture === 'none' && (data.gesture_id !== '' || data.gesture_source !== 'none')) ||
      (data.gesture !== 'none' &&
        (!data.gesture_id ||
          !data.turn_id ||
          data.gesture_source === 'none' ||
          data.state !== 'active')) ||
      (data.gesture_source === 'dialogue' && !['listening', 'speaking'].includes(data.phase)) ||
      (data.state === 'active' && !data.turn_id)
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
  private readonly suspendedSegments = new Map<
    string,
    { instance: string; turnId: string; replyId: string }
  >();
  private readonly blockedTurns = new Set<string>();
  private readonly missedGestures = new Set<string>();
  private readonly knownPresentations = new Map<
    string,
    {
      instance: string;
      turnId: string;
      gestureId: string;
      source: 'command' | 'dialogue';
      phase: DeliveryState['phase'];
      replyId: string;
      segmentId: string;
    }
  >();
  private readonly ackStates = new Map<string, PresentationAck['status']>();
  private ackSink?: (ack: PresentationAck) => void;
  private readonly changeListeners = new Set<() => void>();
  private lastCommand: {
    instance: string;
    turnId: string;
    gesture: AvatarGesture;
  } | null = null;
  private feedbackCommand = '';
  private synchronized = false;
  private presentationInvalidated = false;
  private playbackInvalidated = false;
  private speechAuthorized = false;
  private recoveryFence: {
    instance: string;
    revision: number;
    reply: string | null;
    turn: string | null;
  } | null = null;

  /** 连接代次保留撤权身份；句段墓碑有界，重连仍整回复封存，不能把暂停当作恢复许可。 */
  constructor(previous?: DeliveryGate) {
    this.presentationInvalidated = previous?.presentationInvalidated ?? false;
    this.playbackInvalidated = previous?.playbackInvalidated ?? false;
    if (previous) {
      for (const reply of previous.blockedReplies) this.blockedReplies.add(reply);
      for (const [segment, reply] of previous.suspendedSegments)
        this.suspendedSegments.set(segment, reply);
      for (const turn of previous.blockedTurns) this.blockedTurns.add(turn);
      for (const key of previous.missedGestures) this.missedGestures.add(key);
      this.lastCommand = previous.lastCommand;
    }
    const prior = previous?.current ?? previous?.pending;
    this.recoveryFence = prior
      ? {
          instance: prior.instance,
          revision: Math.max(prior.revision, previous!.consumedRevision),
          reply: prior.state === 'active' ? prior.reply_id : previous!.blockedReply,
          turn:
            prior.state === 'active' &&
            (prior.phase !== 'speaking' || prior.gesture_source === 'command')
              ? prior.turn_id
              : null,
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
        !this.speechBlocked(state) &&
        !this.blockedTurns.has(state.turn_id)
    );
  }

  /** 指令可绕过音频门，但嘴部仍需独立的精确字幕和实际播放许可。 */
  get audibleSpeechAuthorized(): boolean {
    return this.speechAuthorized;
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
          gesture_source: 'none',
        };
    } else if (fence) {
      this.blockedReplies.clear();
      this.suspendedSegments.clear();
      this.blockedTurns.clear();
      this.blockedReply = null;
    }
    // 订阅的新状态仍是实时许可；仅 RPC 返回的历史动作不可在重连后补演。
    if (next === state && state.gesture_id) this.missedGestures.add(this.gestureKey(state));
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

  /** 字幕核对四元组且拒绝已撤旧段；同回复新段仍必须单独绑定，不能借旧字幕恢复。 */
  offer(messages: readonly DeliveryMessage[], agentIdentity: string) {
    const state = this.current;
    if (
      !this.synchronized ||
      !state ||
      state.state !== 'active' ||
      state.phase !== 'speaking' ||
      this.speechBlocked(state) ||
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
        header.turn_id === state.turn_id &&
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

  /** 后端 VAD 可先发布新倾听轮次，本地起声的迟到通知和句内停顿不能再封存它。 */
  interruptForSpeech(): void {
    if (
      this.current?.state === 'active' &&
      this.current.phase === 'listening' &&
      this.current.gesture_source !== 'command'
    )
      this.interruptSpeaking();
    else this.interrupt();
  }

  /** 舞台失效先取消已发力自然动作，避免rAF恢复先报完成；封存后更高修订也不能复活旧轮。 */
  interrupt() {
    this.cancelStartedDialogue();
    if (this.current?.state === 'active') {
      if (this.current.turn_id) this.blockedTurns.add(this.current.turn_id);
      this.cancelCommand();
    }
    this.interruptSpeaking();
    this.candidate = null;
    this.displayed = null;
  }

  /** SDK阶段暂停只撤已实际播放的精确句段；提前到达但尚未出声的新候选仍等待自身PCM。 */
  suspendSpeaking(): void {
    this.cancelStartedDialogue('speaking');
    const state = this.current;
    if (
      state?.state !== 'active' ||
      state.phase !== 'speaking' ||
      !this.displayed ||
      this.displayed.turnId !== state.turn_id ||
      this.displayed.segmentKey !==
        JSON.stringify([state.instance, state.reply_id, state.segment_id])
    )
      return;
    this.suspendSegment(state);
    this.consumedRevision = Math.max(this.consumedRevision, state.revision);
    this.candidate = null;
    this.displayed = null;
    this.speechAuthorized = false;
  }

  /** 能力撤权封整回复，包括工具暂停中的本轮旧回复；恢复后不能把设备故障误当普通阶段续说。 */
  interruptSpeaking() {
    this.cancelStartedDialogue('speaking');
    for (const known of this.suspendedSegments.values())
      if (
        this.current?.state === 'active' &&
        known.instance === this.current.instance &&
        known.turnId === this.current.turn_id
      )
        this.blockedReplies.add(known.replyId);
    if (this.current?.state === 'active' && this.current.phase === 'speaking') {
      this.blockedReply = this.current.reply_id;
      this.blockedReplies.add(this.current.reply_id);
      this.consumedRevision = Math.max(this.consumedRevision, this.current.revision);
      this.candidate = null;
      this.displayed = null;
      this.speechAuthorized = false;
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
    this.speechAuthorized = false;
    if (!visualAvailable || !this.synchronized || state?.state !== 'active') return null;
    if (this.blockedTurns.has(state.turn_id)) return null;
    if (state.phase !== 'speaking') return this.delivery(state);
    const independent = state.gesture_source === 'command' ? this.delivery(state) : null;
    if (!playbackAvailable || this.speechBlocked(state)) return independent;
    const sameSegment =
      this.displayed?.turnId === state.turn_id &&
      this.displayed?.segmentKey ===
        JSON.stringify([state.instance, state.reply_id, state.segment_id]);
    if (this.candidate && (audible || sameSegment)) {
      this.displayed = this.candidate;
      this.candidate = null;
      this.consumedRevision = state.revision;
    }
    this.speechAuthorized = Boolean(this.displayed);
    return independent ?? this.displayed;
  }

  /** 权威状态离开旧段即封其身份；工具新段可独立绑定，已撤旧段不能靠更高修订复活。 */
  private apply(state: DeliveryState) {
    const previous = this.current;
    const sameSegment =
      previous?.state === 'active' &&
      state.state === 'active' &&
      previous.instance === state.instance &&
      previous.turn_id === state.turn_id &&
      previous.phase === 'speaking' &&
      state.phase === 'speaking' &&
      previous.reply_id === state.reply_id &&
      previous.segment_id === state.segment_id;
    if (previous?.state === 'active' && previous.phase === 'speaking' && !sameSegment)
      this.suspendSegment(previous);
    this.current = state;
    if (
      state.state === 'active' &&
      state.gesture !== 'none' &&
      state.gesture_id &&
      state.gesture_source !== 'none' &&
      !this.blockedTurns.has(state.turn_id) &&
      !this.missedGestures.has(this.gestureKey(state))
    ) {
      this.knownPresentations.set(this.gestureKey(state), {
        instance: state.instance,
        turnId: state.turn_id,
        gestureId: state.gesture_id,
        source: state.gesture_source,
        phase: state.phase,
        replyId: state.reply_id,
        segmentId: state.segment_id,
      });
      if (state.gesture_source === 'command')
        this.lastCommand = {
          instance: state.instance,
          turnId: state.turn_id,
          gesture: state.gesture,
        };
      if (this.knownPresentations.size > 64)
        this.knownPresentations.delete(this.knownPresentations.keys().next().value!);
    }
    this.candidate = null;
    if (!sameSegment) {
      this.displayed = null;
      this.speechAuthorized = false;
    }
    // 更高序号的新回复已经证明旧回复失去所有权，不依赖某个关闭通知一定送达。
    if (state.state === 'active' && this.blockedReply && state.reply_id !== this.blockedReply) {
      this.blockedReply = null;
    }
    const commandKey = state.gesture_source === 'command' ? this.gestureKey(state) : '';
    const retryGesture =
      this.lastCommand?.instance === state.instance && this.lastCommand.turnId === state.turn_id
        ? this.lastCommand.gesture
        : null;
    if (
      this.feedback.feedback !== state.command_feedback ||
      this.feedback.gesture !== retryGesture ||
      this.feedbackCommand !== commandKey
    ) {
      this.feedbackCommand = commandKey;
      this.feedback = { feedback: state.command_feedback, gesture: retryGesture };
      for (const listener of this.changeListeners) listener();
    }
  }

  /** 最多保留64个精确旧段；容量耗尽时将最旧身份提升为整回复封存，不能淘汰后重新放行。 */
  private suspendSegment(state: DeliveryState): void {
    if (!state.reply_id || !state.segment_id) return;
    const key = JSON.stringify([state.instance, state.turn_id, state.reply_id, state.segment_id]);
    this.suspendedSegments.set(key, {
      instance: state.instance,
      turnId: state.turn_id,
      replyId: state.reply_id,
    });
    if (this.suspendedSegments.size > 64) {
      const [oldKey, oldSegment] = this.suspendedSegments.entries().next().value!;
      this.blockedReplies.add(oldSegment.replyId);
      this.suspendedSegments.delete(oldKey);
    }
  }

  /** 整回复撤权与句段暂停共享同一许可判断，音频、字幕及自然started不能走不同的恢复规则。 */
  private speechBlocked(
    state: Pick<DeliveryState, 'instance' | 'turn_id' | 'reply_id' | 'segment_id'>
  ): boolean {
    return (
      this.blockedReplies.has(state.reply_id) ||
      this.suspendedSegments.has(
        JSON.stringify([state.instance, state.turn_id, state.reply_id, state.segment_id])
      )
    );
  }

  /** 稳定动作身份与可变视觉版本分开，重复字幕或表情变化不会重新启动已消费手势。 */
  private delivery(state: DeliveryState): AvatarDelivery {
    return {
      id: `${state.instance}:${state.revision}:${state.segment_id}`,
      instance: state.instance,
      replyKey: JSON.stringify([state.instance, state.reply_id || state.turn_id]),
      segmentKey: JSON.stringify([state.instance, state.reply_id, state.segment_id]),
      phase: state.phase,
      turnId: state.turn_id,
      gestureId: this.missedGestures.has(this.gestureKey(state)) ? '' : state.gesture_id,
      gestureSource: this.missedGestures.has(this.gestureKey(state))
        ? 'none'
        : state.gesture_source,
      energy: state.energy,
      style: state.style,
      gesture: this.missedGestures.has(this.gestureKey(state)) ? 'none' : state.gesture,
    };
  }

  /** RPC 仅有当前连接所有者可发送；舞台通过此端口报告真实渲染结果。 */
  setAcknowledgmentSink(sink?: (ack: PresentationAck) => void): void {
    this.ackSink = sink;
  }

  /** 实时身份和终态门不变；可选stroke事实只校验不猜测，缺started仍不能合成false。 */
  acknowledge(ack: PresentationAck): void {
    if (
      (ack.stroke_started !== undefined && typeof ack.stroke_started !== 'boolean') ||
      (ack.stroke_started === false && ['started', 'completed'].includes(ack.status))
    )
      return;
    const key = JSON.stringify([ack.instance, ack.turn_id, ack.gesture_id]);
    const known = this.knownPresentations.get(key);
    if (!known) return;
    const previous = this.ackStates.get(key);
    if (previous === ack.status || (previous && previous !== 'started')) return;
    const current = this.current;
    const sameTurn =
      current?.state === 'active' &&
      current.instance === ack.instance &&
      current.turn_id === ack.turn_id;
    if (!sameTurn && (ack.status === 'started' || previous !== 'started')) return;
    if (
      ack.status === 'started' &&
      (this.blockedTurns.has(ack.turn_id) ||
        this.missedGestures.has(key) ||
        (known.source === 'dialogue' &&
          (current?.phase !== known.phase ||
            (known.phase === 'speaking' &&
              (this.speechBlocked({
                instance: known.instance,
                turn_id: known.turnId,
                reply_id: known.replyId,
                segment_id: known.segmentId,
              }) ||
                current?.reply_id !== known.replyId ||
                current?.segment_id !== known.segmentId)) ||
            (known.phase === 'listening' && current?.gesture_id !== known.gestureId))))
    )
      return;
    if (ack.status === 'completed' && previous !== 'started') return;
    this.ackStates.set(key, ack.status);
    if (this.ackStates.size > 64) this.ackStates.delete(this.ackStates.keys().next().value!);
    this.ackSink?.(ack);
  }

  /** 只有已收到真实started才合成true取消；未开始候选由renderer给事实，不以缺ACK猜false。 */
  private cancelStartedDialogue(phase?: 'speaking'): void {
    for (const [key, known] of this.knownPresentations) {
      if (
        known.source === 'dialogue' &&
        (!phase || known.phase === phase) &&
        this.ackStates.get(key) === 'started'
      )
        this.acknowledge({
          instance: known.instance,
          turn_id: known.turnId,
          gesture_id: known.gestureId,
          status: 'canceled',
          stroke_started: true,
        });
    }
  }

  /** 静态降级也闭环明确指令，但不能把错过的快照当作新指令报告失败。 */
  unavailable(): void {
    const current = this.current;
    if (current?.gesture_source === 'command')
      this.acknowledge({
        instance: current.instance,
        turn_id: current.turn_id,
        gesture_id: current.gesture_id,
        status: 'unavailable',
      });
  }

  /** 取消独立指令不依赖音频阶段，页面隐藏时也不会伪称完成。 */
  private cancelCommand(): void {
    const current = this.current;
    if (current?.gesture_source === 'command')
      this.acknowledge({
        instance: current.instance,
        turn_id: current.turn_id,
        gesture_id: current.gesture_id,
        status: 'canceled',
      });
  }

  /** 协议身份使用结构编码，实例、轮次或手势字符串中的分隔符不能碰撞。 */
  private gestureKey(state: DeliveryState): string {
    return JSON.stringify([state.instance, state.turn_id, state.gesture_id]);
  }

  /** React 仅订阅低频命令反馈，逐帧动画继续留在渲染器中。 */
  subscribe = (listener: () => void): (() => void) => {
    this.changeListeners.add(listener);
    return () => this.changeListeners.delete(listener);
  };

  /** 仅同实例同轮的实时指令可供主动重试；未知失败保留状态提示，不由历史动作猜测。 */
  getFeedback = (): {
    feedback: DeliveryState['command_feedback'];
    gesture: AvatarGesture | null;
  } => this.feedback;
  private feedback: { feedback: DeliveryState['command_feedback']; gesture: AvatarGesture | null } =
    { feedback: 'none', gesture: null };
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
