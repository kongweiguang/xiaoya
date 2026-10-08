import {
  ConnectionState,
  DataPacket_Kind,
  ParticipantKind,
  type RemoteParticipant,
  type Room,
  RoomEvent,
} from 'livekit-client';
import { DeliveryGate, type DeliveryMessage, parseDelivery } from './avatar/delivery';

/** 房间和两端 SID 共同界定控制通道；就绪和动画共享一次权威快照与取消边界。 */
export class DeliveryChannel {
  readonly gate: DeliveryGate;
  readonly ready: Promise<boolean>;
  readonly localSid: string;
  readonly agentSid: string;
  private readonly controller = new AbortController();
  private finishReady: (ready: boolean) => void = () => undefined;
  private finished = false;
  isReady = false;
  private messages: readonly DeliveryMessage[] = [];
  private playbackUnavailable: boolean;
  private hidden: boolean;

  /** 先订阅后读快照，确保开场在 presence 广播和 RPC 交错时也不会丢失或补演。 */
  constructor(
    readonly room: Room,
    readonly agent: RemoteParticipant,
    previous: DeliveryGate,
    private readonly invalidated: () => void
  ) {
    this.localSid = room.localParticipant.sid;
    this.agentSid = agent.sid;
    this.gate = new DeliveryGate(previous);
    this.playbackUnavailable = !room.canPlaybackAudio;
    this.hidden = document.hidden;
    this.ready = new Promise((resolve) => {
      this.finishReady = resolve;
    });
    room.on(RoomEvent.DataReceived, this.receive);
    room.on(RoomEvent.ConnectionStateChanged, this.connectionChanged);
    room.on(RoomEvent.ParticipantDisconnected, this.participantLeft);
    room.on(RoomEvent.ActiveSpeakersChanged, this.activeSpeakers);
    room.on(RoomEvent.AudioPlaybackStatusChanged, this.playbackChanged);
    document.addEventListener('visibilitychange', this.visibilityChanged);
    if (this.playbackUnavailable || this.hidden) this.gate.invalidatePresentation();
    void this.synchronize();
  }

  /** SDK 可在同一个 Room 内更换 SID，引用相同不能沿用旧用户的许可。 */
  matches(agent: RemoteParticipant): boolean {
    return (
      !this.controller.signal.aborted &&
      this.agent.identity === agent.identity &&
      this.agentSid === agent.sid &&
      this.currentConnection()
    );
  }

  /** 字幕只为受控表现提供语义锚点，逐帧门控仍由真实可听音频决定。 */
  offer(messages: readonly DeliveryMessage[]): void {
    this.messages = messages;
    this.gate.offer(messages, this.agent.identity);
  }

  /** 关闭先令异步结果失效，再解除监听；迟到 RPC 不得写入新通道。 */
  close(): void {
    if (this.controller.signal.aborted) return;
    this.controller.abort();
    this.complete(false);
    this.gate.interrupt();
    this.room.off(RoomEvent.DataReceived, this.receive);
    this.room.off(RoomEvent.ConnectionStateChanged, this.connectionChanged);
    this.room.off(RoomEvent.ParticipantDisconnected, this.participantLeft);
    this.room.off(RoomEvent.ActiveSpeakersChanged, this.activeSpeakers);
    this.room.off(RoomEvent.AudioPlaybackStatusChanged, this.playbackChanged);
    document.removeEventListener('visibilitychange', this.visibilityChanged);
  }

  /** Agent 必须仍是 SDK 成员表中的当前实例，消息正文中的身份不参与授权。 */
  private currentConnection(): boolean {
    const current = this.room.remoteParticipants.get(this.agent.identity);
    return (
      this.room.state === ConnectionState.Connected &&
      !!this.localSid &&
      this.room.localParticipant.sid === this.localSid &&
      current?.sid === this.agentSid &&
      current.kind === ParticipantKind.AGENT
    );
  }

  /** 成功、超时和关闭只兑现一次就绪结果，保留数据订阅服务后续句段。 */
  private complete(ready: boolean): void {
    if (this.finished) return;
    this.finished = true;
    this.isReady = ready;
    this.finishReady(ready);
  }

  /** presence 可能按三秒批次广播；统一五秒预算，取消立即结束而非等 SDK 的迟到响应。 */
  private async synchronize(): Promise<void> {
    const deadline = setTimeout(() => {
      this.complete(false);
      this.close();
    }, 5_000);
    try {
      for (let attempt = 0; attempt < 5 && !this.controller.signal.aborted; attempt++) {
        if (!this.currentConnection()) return this.complete(false);
        try {
          const response = await this.room.localParticipant.performRpc({
            destinationIdentity: this.agent.identity,
            method: 'xiaoya.getDeliverySnapshot',
            payload: '',
            responseTimeout: 1_000,
          });
          if (this.controller.signal.aborted) return;
          const snapshot = parseDelivery(response);
          if (!snapshot || !this.currentConnection()) return this.complete(false);
          this.gate.snapshot(snapshot);
          this.offer(this.messages);
          return this.complete(true);
        } catch {
          if (attempt === 4 || !this.currentConnection()) return this.complete(false);
          await this.wait(400 * (attempt + 1));
        }
      }
    } finally {
      clearTimeout(deadline);
    }
  }

  /** 排队间隔只属于当前通道，关闭后不留下计时器或悬挂 Promise。 */
  private wait(delay: number): Promise<void> {
    return new Promise((resolve) => {
      /** 到期和取消共用清理，重试不累积 AbortSignal 监听。 */
      const finish = () => {
        clearTimeout(timer);
        this.controller.signal.removeEventListener('abort', finish);
        resolve();
      };
      const timer = setTimeout(finish, delay);
      this.controller.signal.addEventListener('abort', finish, { once: true });
      if (this.controller.signal.aborted) finish();
    });
  }

  /** 可靠包须来自当前真实 Agent；旧 SID、其他参与者和损坏包均不能改变动作。 */
  private receive = (
    payload: Uint8Array,
    participant?: RemoteParticipant,
    kind?: DataPacket_Kind,
    topic?: string
  ): void => {
    if (
      this.controller.signal.aborted ||
      !this.currentConnection() ||
      topic !== 'xiaoya.delivery' ||
      kind !== DataPacket_Kind.RELIABLE ||
      participant?.kind !== ParticipantKind.AGENT ||
      participant.identity !== this.agent.identity ||
      participant.sid !== this.agentSid
    )
      return;
    const state = parseDelivery(new TextDecoder().decode(payload));
    if (state) {
      this.gate.receive(state);
      this.offer(this.messages);
    }
  };

  /** 真正离开 connected 就撤销快照；恢复必须重新建立控制通道。 */
  private connectionChanged = (state: ConnectionState): void => {
    if (state !== ConnectionState.Connected) {
      this.close();
      this.invalidated();
    }
  };

  /** 无关参与者离场不影响当前会话，Agent 换代则不能保留旧许可。 */
  private participantLeft = (participant: RemoteParticipant): void => {
    if (participant.identity === this.agent.identity && participant.sid === this.agentSid) {
      this.close();
      this.invalidated();
    }
  };

  /** 用户开口先截断动作，不等待转写或模型响应。 */
  private activeSpeakers = (): void => {
    if (this.room.localParticipant.isSpeaking) this.gate.interrupt();
  };

  /** 播放许可丢失和恢复都划定新的表现边界，恢复后不追赶旧手势。 */
  private playbackChanged = (): void => {
    const unavailable = !this.room.canPlaybackAudio;
    if (unavailable === this.playbackUnavailable) return;
    this.playbackUnavailable = unavailable;
    if (unavailable) this.gate.invalidatePresentation();
    else this.gate.interrupt();
  };

  /** 后台回复已错过展示时机，返回前台只允许之后的新回复表演。 */
  private visibilityChanged = (): void => {
    const hidden = document.hidden;
    if (hidden === this.hidden) return;
    this.hidden = hidden;
    if (hidden) this.gate.invalidatePresentation();
    else this.gate.interrupt();
  };
}
