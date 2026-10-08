import {
  ConnectionState,
  type LocalAudioTrack,
  type LocalTrack,
  ParticipantKind,
  type RemoteParticipant,
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
} from 'livekit-client';
import type { UseSessionReturn } from '@livekit/components-react';
import { type SessionAudio, createSessionAudio } from './avatar/audio-bridge';
import { DeliveryGate, type SubtitleMessage } from './avatar/delivery';
import { acknowledgeConversationEnd } from './conversation-end';
import { DeliveryChannel } from './delivery-channel';

export type ConversationPhase = 'idle' | 'connecting' | 'active' | 'recovering' | 'ending';
export type ConversationPeer = { state: string; agent?: RemoteParticipant; track?: Track };
type StartupAgent = { waitUntilConnected: (signal: AbortSignal) => Promise<void> };
export type ConversationSnapshot = {
  phase: ConversationPhase;
  error: string;
  microphoneFailed: boolean;
  microphoneCapturing: boolean;
  conversationId: number;
  attempt: ConversationAttempt | null;
  peer: ConversationPeer;
  messages: readonly SubtitleMessage[];
  sending: boolean;
};

/** 可操作的设备错误与连接错误分开，不能把令牌、地址或外部异常正文显示到页面。 */
export function microphoneError(error: unknown): string {
  const name = error instanceof Error ? error.name : '';
  return /NotAllowed|PermissionDenied/.test(name)
    ? '麦克风权限被拒绝。请允许浏览器使用麦克风，或改用文字聊天。'
    : '麦克风暂不可用。请检查设备后重试，或继续使用文字聊天。';
}

/** 当前公开状态决定可交互性，SDK 历史 failed 标记不能否定已恢复的 Agent。 */
function assistantAvailable(state: string): boolean {
  return state === 'listening' || state === 'thinking' || state === 'speaking';
}

/** 页面历史只保留文字值，不持有 SDK Participant 的信令、轨道与旧 Room 资源图。 */
function snapshotMessage(message: SubtitleMessage): SubtitleMessage {
  return {
    id: message.id,
    message: message.message,
    timestamp: message.timestamp,
    attributes: message.attributes ? { ...message.attributes } : undefined,
    from: message.from
      ? { identity: message.from.identity, isLocal: message.from.isLocal }
      : undefined,
  };
}

/** 页面订阅一个稳定快照；资源和连接代次留在唯一 Attempt，不散布到多个 React ref。 */
export class ConversationController {
  readonly delivery = { current: new DeliveryGate() };
  private listeners = new Set<() => void>();
  private serial = 0;
  private mounts = 0;
  private snapshot: ConversationSnapshot = {
    phase: 'idle',
    error: '',
    microphoneFailed: false,
    microphoneCapturing: false,
    conversationId: 0,
    attempt: null,
    peer: { state: 'disconnected' },
    messages: [],
    sending: false,
  };

  /** 外部存储要求未变化时保持对象身份，避免 SDK 观察结果导致无意义渲染循环。 */
  getSnapshot = (): ConversationSnapshot => this.snapshot;

  /** 同一页面订阅集中解除，不给每个控件独立创建媒体或连接监听。 */
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** StrictMode 的模拟卸载可在同一微任务重新挂载；真正离开页面仍集中关闭当前尝试。 */
  mount(): () => void {
    this.mounts++;
    return () => {
      this.mounts--;
      queueMicrotask(() => {
        if (!this.mounts) void this.end();
      });
    };
  }

  /** 只在明确点击时创建 Room 和音频时钟，保留用户手势以申请权限与恢复播放。 */
  start = (withMicrophone = true): void => {
    if (this.snapshot.attempt) return;
    let audio: SessionAudio | undefined;
    try {
      audio = createSessionAudio();
      const room = new Room({
        singlePeerConnection: false,
        webAudioMix: { audioContext: audio.context },
      });
      const attempt = new ConversationAttempt(++this.serial, room, audio, withMicrophone, this);
      this.delivery.current = new DeliveryGate();
      this.update({
        phase: 'connecting',
        error: '',
        microphoneFailed: false,
        microphoneCapturing: false,
        conversationId: attempt.id,
        attempt,
        peer: { state: 'connecting' },
        sending: false,
      });
      void audio.context.resume().catch(() => undefined);
    } catch {
      if (audio) void audio.close().catch(() => undefined);
      this.update({ error: '当前浏览器无法准备聊天，请检查设备后重试。' });
    }
  };

  /** 双击、取消和故障回收共享当前尝试的关闭 Promise，不影响之后的新会话。 */
  end = (): Promise<void> => this.snapshot.attempt?.close() ?? Promise.resolve();

  /** 文字只能送到当前已经确认的连接；恢复不会自动重发页面保存的草稿。 */
  send = async (text: string): Promise<unknown> => {
    const attempt = this.snapshot.attempt;
    if (!attempt || this.snapshot.phase !== 'active') throw new Error('聊天连接尚未准备好');
    return attempt.send(text);
  };

  /** 所有异步写入携带其资源所有者，旧关闭、旧确认及旧消息不能覆盖新尝试。 */
  change(attempt: ConversationAttempt, change: Partial<ConversationSnapshot>): void {
    if (this.snapshot.attempt === attempt) this.update(change);
  }

  /** 释放后保留当前会话字幕与错误，方便用户阅读并重新连接。 */
  released(attempt: ConversationAttempt): void {
    this.change(attempt, {
      phase: 'idle',
      attempt: null,
      peer: { state: 'disconnected' },
      sending: false,
      microphoneCapturing: false,
    });
  }

  /** 快照仅在可见数据变化时替换，监听器能安全重新订阅同一个控制器。 */
  private update(change: Partial<ConversationSnapshot>): void {
    if (
      Object.entries(change).every(([key, value]) =>
        Object.is(this.snapshot[key as keyof ConversationSnapshot], value)
      )
    )
      return;
    this.snapshot = { ...this.snapshot, ...change };
    for (const listener of this.listeners) listener();
  }
}

/** 一次尝试独占采集、Room、时钟、取消与关闭；组件只借用这些资源。 */
export class ConversationAttempt {
  readonly cancellation = new AbortController();
  private session?: UseSessionReturn;
  private channel?: DeliveryChannel;
  private peer: ConversationPeer = { state: 'connecting' };
  private track?: LocalAudioTrack;
  private readonly trackReady: Promise<LocalAudioTrack | undefined>;
  private sendText?: (message: string) => Promise<unknown>;
  private closing?: Promise<void>;
  private started = false;
  private active = false;
  private confirming?: DeliveryChannel;
  private timer?: ReturnType<typeof setTimeout>;
  private recoveryTimer?: ReturnType<typeof setTimeout>;
  private readonly stopped = new Set<LocalTrack>();
  private readonly history: readonly SubtitleMessage[];
  private lastMessages?: readonly SubtitleMessage[];
  private mergedMessages: readonly SubtitleMessage[];

  /** 权限请求立即开始但不发布，取消后迟到的许可只产生可回收的轨道。 */
  constructor(
    readonly id: number,
    readonly room: Room,
    readonly audio: SessionAudio,
    withMicrophone: boolean,
    private readonly owner: ConversationController
  ) {
    this.history = owner.getSnapshot().messages;
    this.mergedMessages = this.history;
    this.trackReady = withMicrophone
      ? createLocalAudioTrack().then((track) => {
          this.track = track;
          if (this.cancellation.signal.aborted) this.stopTrack(track);
          return track;
        })
      : Promise.resolve(undefined);
    // 权限拒绝可能先于 SessionHost 提交；在 connect 真正消费结果前也必须处理拒绝。
    void this.trackReady.catch(() => undefined);
    this.timer = setTimeout(() => {
      if (this.active || this.cancellation.signal.aborted) return;
      this.fail('连接超过 30 秒，请检查服务后重试。');
    }, 30_000);
  }

  /** SessionHost 提交且 SDK 订阅已建立后启动，StrictMode 不会重复发起令牌与采集。 */
  connect(session: UseSessionReturn, startupAgent: StartupAgent): void {
    if (this.started || this.cancellation.signal.aborted) return;
    this.started = true;
    this.session = session;
    void this.run(startupAgent);
  }

  /** SDK 观察器只投递公开状态；真正恢复必须复核两端 SID 并共享权威快照。 */
  observe(
    peer: ConversationPeer,
    messages: readonly SubtitleMessage[],
    sending: boolean,
    send: (message: string) => Promise<unknown>
  ): void {
    if (this.cancellation.signal.aborted) return;
    this.sendText = send;
    if (
      this.peer.state !== peer.state ||
      this.peer.agent !== peer.agent ||
      this.peer.track !== peer.track
    ) {
      this.peer = peer;
      this.owner.change(this, { peer });
    }
    if (this.lastMessages !== messages) {
      this.lastMessages = messages;
      this.mergedMessages = messages.length
        ? [...this.history, ...messages.map(snapshotMessage)]
        : this.history;
    }
    this.owner.change(this, { messages: this.mergedMessages, sending });
    this.channel?.offer(this.mergedMessages);
    if (this.active) this.recover();
  }

  /** SDK 可变状态每次重读，active 旧渲染不能把文字发送到已断开的房间。 */
  send(text: string): Promise<unknown> {
    if (
      !this.active ||
      !this.peer.agent ||
      !this.channel?.isReady ||
      !this.channel.matches(this.peer.agent) ||
      !this.sendText
    )
      return Promise.reject(new Error('连接未就绪'));
    return this.sendText(text);
  }

  /** 控件从实际 SDK 轨道投影采集能力；旧尝试和结束后的结果不能让页面重新承诺收音。 */
  reportMicrophone = (capturing: boolean): void => {
    if (!this.cancellation.signal.aborted)
      this.owner.change(this, { microphoneCapturing: capturing });
  };

  /** 先停止采集，再有界确认结束，最后关闭 SDK 和借出的音频时钟。 */
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.cancellation.abort();
    clearTimeout(this.timer);
    clearTimeout(this.recoveryTimer);
    this.channel?.close();
    this.active = false;
    this.stopMicrophone();
    this.owner.change(this, { phase: 'ending', microphoneCapturing: false });
    this.closing = this.release();
    return this.closing;
  }

  /** 取消可立即退出不可取消的权限或令牌等待，底层迟到结果仍留在原资源所有者内。 */
  private async run(startupAgent: StartupAgent): Promise<void> {
    const signal = this.cancellation.signal;
    /** 监听尚未登记时也可安全清理；真正的取消回调在 Promise 建立后替换占位值。 */
    let abort: () => void = () => undefined;
    const aborted = new Promise<never>((_, reject) => {
      abort = () => reject(new DOMException('连接已取消', 'AbortError'));
      signal.addEventListener('abort', abort, { once: true });
    });
    this.room.on(RoomEvent.Connected, this.lateConnection);
    /** SDK 连接最终完成后才解除迟到回收，关闭返回不能使尚在请求中的旧房间复活。 */
    const pending = this.establish(startupAgent).finally(() => {
      this.room.off(RoomEvent.Connected, this.lateConnection);
      if (signal.aborted) return this.room.disconnect().catch(() => undefined);
    });
    try {
      await Promise.race([pending, aborted]);
      signal.throwIfAborted();
      clearTimeout(this.timer);
      this.active = true;
      this.owner.change(this, { phase: 'active' });
    } catch (failure) {
      if (signal.aborted) return;
      const name = failure instanceof Error ? failure.name : '';
      const deviceFailure = /NotAllowed|NotFound|NotReadable|PermissionDenied|Overconstrained/.test(
        name
      );
      this.owner.change(this, { microphoneFailed: deviceFailure });
      this.fail(deviceFailure ? microphoneError(failure) : '连接失败，请稍后重试。');
    } finally {
      signal.removeEventListener('abort', abort);
    }
  }

  /** 权限、房间、发布和 Agent 顺序固定；快照确认是唯一可交互的授权点。 */
  private async establish(startupAgent: StartupAgent): Promise<void> {
    const signal = this.cancellation.signal;
    const track = await this.trackReady;
    signal.throwIfAborted();
    await this.session!.start({
      signal,
      tracks: { microphone: { enabled: false }, camera: { enabled: false } },
    });
    signal.throwIfAborted();
    if (track)
      await this.room.localParticipant.publishTrack(track, { source: Track.Source.Microphone });
    signal.throwIfAborted();
    await startupAgent.waitUntilConnected(signal);
    signal.throwIfAborted();
    const agent =
      this.peer.agent ??
      Array.from(this.room.remoteParticipants.values()).find(
        (participant) =>
          participant.kind === ParticipantKind.AGENT &&
          !('lk.publish_on_behalf' in participant.attributes)
      );
    if (!agent || !(await this.ensureChannel(agent).ready)) throw new Error('聊天连接尚未准备好');
    signal.throwIfAborted();
    if (!this.channel?.matches(agent)) throw new Error('聊天连接已变化');
  }

  /** 每个当前 SID 组合只建一次通道；动画和发送许可从同一个控制器读取快照。 */
  private ensureChannel(agent: RemoteParticipant): DeliveryChannel {
    if (this.channel?.matches(agent)) return this.channel;
    this.channel?.close();
    this.channel = new DeliveryChannel(this.room, agent, this.owner.delivery.current, () => {
      if (this.active && !this.cancellation.signal.aborted) this.recover();
    });
    this.owner.delivery.current = this.channel.gate;
    this.channel.offer(this.owner.getSnapshot().messages);
    return this.channel;
  }

  /** 网络与 Agent 共享一个三十秒恢复窗口，状态来回切换不重置截止点。 */
  private recover(): void {
    if (this.cancellation.signal.aborted) return;
    if (this.room.state === ConnectionState.Disconnected)
      return this.fail('聊天连接已断开，可以重新连接。');
    const agent = this.peer.agent;
    if (
      this.room.state === ConnectionState.Connected &&
      agent &&
      assistantAvailable(this.peer.state)
    ) {
      const channel = this.ensureChannel(agent);
      if (channel.isReady) {
        clearTimeout(this.recoveryTimer);
        this.recoveryTimer = undefined;
        this.owner.change(this, { phase: 'active' });
        return;
      }
      if (this.confirming !== channel) {
        this.confirming = channel;
        /** 字幕频繁变化不重复登记恢复等待；迟到结果只能改变本尝试的当前通道。 */
        void channel.ready.then((ready) => {
          if (this.confirming === channel) this.confirming = undefined;
          if (this.cancellation.signal.aborted || this.channel !== channel) return;
          if (ready) this.recover();
          else this.fail('恢复连接未完成，可以重新连接。');
        });
      }
    }
    this.owner.change(this, { phase: 'recovering' });
    this.recoveryTimer ??= setTimeout(() => {
      // SDK 事件可能先于 React 清理，已确认的真实恢复不能被排队中的旧计时器挂断。
      if (
        this.peer.agent &&
        this.channel?.isReady &&
        this.channel.matches(this.peer.agent) &&
        assistantAvailable(this.peer.state)
      )
        this.recover();
      else this.fail('恢复连接超时，请重新连接。');
    }, 30_000);
  }

  /** 错误保留为页面状态，回收无论成功与否都会执行。 */
  private fail(message: string): void {
    if (this.cancellation.signal.aborted) return;
    this.owner.change(this, { error: message });
    void this.close();
  }

  /** 相同采集可能同时存在于权限结果和 publication，释放只执行一次。 */
  private stopTrack(track: LocalTrack): void {
    if (this.stopped.has(track)) return;
    this.stopped.add(track);
    track.stop();
  }

  /** 只停止本地采集，远端音轨生命周期继续由 SDK 持有。 */
  private stopMicrophone(): void {
    if (this.track) this.stopTrack(this.track);
    this.room.localParticipant.audioTrackPublications.forEach((publication) => {
      if (publication.track) this.stopTrack(publication.track);
    });
  }

  /** 可靠结束确认失败不能阻止断开；时钟必须在 SDK 释放播放器之后关闭。 */
  private async release(): Promise<void> {
    try {
      await acknowledgeConversationEnd(this.room, this.peer.agent).catch(() => false);
      if (this.session) await this.session.end();
      else await this.room.disconnect();
    } catch {
      this.owner.change(this, { error: '连接关闭异常，请重新连接。' });
      await this.room.disconnect().catch(() => undefined);
    } finally {
      this.stopMicrophone();
      await this.audio.close().catch(() => undefined);
      this.owner.released(this);
    }
  }

  /** 令牌请求或 SDK 连接取消后迟到成功，只断开它原先的房间。 */
  private lateConnection = (): void => {
    if (this.cancellation.signal.aborted) void this.room.disconnect().catch(() => undefined);
  };
}
