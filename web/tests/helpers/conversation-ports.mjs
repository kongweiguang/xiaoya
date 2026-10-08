import { EventEmitter } from 'node:events';
import { React } from './dom.mjs';

export const events = {
  Connected: 'connected',
  DataReceived: 'data',
  ConnectionStateChanged: 'connection',
  ParticipantDisconnected: 'participant-left',
  ActiveSpeakersChanged: 'speakers',
  AudioPlaybackStatusChanged: 'playback',
};
export const states = {
  Connected: 'connected',
  Disconnected: 'disconnected',
  Reconnecting: 'reconnecting',
  SignalReconnecting: 'signal-reconnecting',
};

/** 合法权威状态采用当前协议，不以任意对象绕过生产解析与许可规则。 */
export function delivery(revision = 0, overrides = {}) {
  return {
    v: 1,
    instance: 'job-a',
    revision,
    reply_id: '',
    segment_id: '',
    state: 'closed',
    style: 'neutral',
    gesture: 'none',
    ...overrides,
  };
}

/** 仅读写公开轨道状态；权限、设备与媒体停止继续由真实控件负责。 */
export class LocalTrack extends EventEmitter {
  isMuted = false;
  stops = 0;
  mediaStreamTrack = Object.assign(new window.EventTarget(), {
    kind: 'audio',
    readyState: 'live',
    enabled: true,
  });
  /** 重复 SDK 关闭与主动停止均允许，Ended 状态不会再变回 live。 */
  stop() {
    this.stops++;
    this.mediaStreamTrack.readyState = 'ended';
    this.mediaStreamTrack.enabled = false;
  }
}
export class RemoteTrack extends LocalTrack {}

/** SDK 与浏览器端口可以失序完成；React 渲染、生产控制器和表现通道不被替换。 */
export function conversationPorts(options = {}) {
  const rooms = [];
  const contexts = [];
  const tracks = [];
  const calls = { microphones: [], sessionOptions: [], renderers: 0 };
  const Session = React.createContext(null);
  const agent = {
    identity: 'agent',
    sid: 'PA_agent',
    kind: 4,
    attributes: { 'lk.agent.state': 'listening' },
  };
  class Room extends EventEmitter {
    state = 'disconnected';
    canPlaybackAudio = true;
    remoteParticipants = new Map();
    listeners = new Set();
    deviceId = 'device-a';
    rpcCalls = [];
    published = [];
    switched = [];
    unpublish = [];
    disconnects = 0;
    starts = 0;
    ends = 0;
    messages = [];
    sending = false;
    peer = { state: 'disconnected', agent: undefined, track: undefined };
    /** 每次用户操作建立一个资源对象，测试能观察点击前零分配及 SDK 配置。 */
    constructor(config) {
      super();
      this.config = config;
      rooms.push(this);
      this.localParticipant = {
        sid: '',
        identity: 'user',
        isSpeaking: false,
        audioTrackPublications: new Map(),
        /** 记录订阅先后并允许失败或迟到响应，快照解析仍执行生产代码。 */
        performRpc: async (request) => {
          this.rpcCalls.push({
            ...request,
            subscribed: this.listenerCount(events.DataReceived) > 0,
          });
          if (this.rpcGate) return this.rpcGate.promise;
          if (this.rpcResponses?.length) {
            const value = this.rpcResponses.shift();
            if (value instanceof Error) throw value;
            return value;
          }
          return JSON.stringify(delivery());
        },
        /** 发布和权限分开控制，取消后的 publication 仍须由真实资源所有者回收。 */
        publishTrack: async (track) => {
          if (this.publishGate) await this.publishGate.promise;
          this.published.push(track);
          this.localParticipant.audioTrackPublications.set('microphone', {
            track,
            audioTrack: track,
          });
          this.notify();
        },
        /** SDK 协商失败可能晚于 map 删除，控件需要重新读取实际 publication。 */
        unpublishTrack: async (track) => {
          this.unpublish.push(track);
          this.localParticipant.audioTrackPublications.delete('microphone');
          this.notify();
          if (this.unpublishFailure) throw this.unpublishFailure;
        },
        getTrackPublication: () => this.localParticipant.audioTrackPublications.get('microphone'),
        /** 切换错误可使旧媒体结束，不能让 publication 缓存伪装成正在采集。 */
        setMicrophoneEnabled: async (enabled) => {
          const track = this.localParticipant.getTrackPublication()?.track;
          if (this.enableFailure) {
            track?.stop();
            throw this.enableFailure;
          }
          if (track) {
            track.isMuted = !enabled;
            track.mediaStreamTrack.enabled = enabled;
          }
          this.notify();
        },
        /** 结束 ACK 由真实控制包决定，不直接兑现生产等待器。 */
        publishData: async (bytes) => {
          const packet = JSON.parse(new TextDecoder().decode(bytes));
          if (this.acknowledge !== false && packet.type === 'user_end')
            queueMicrotask(() =>
              this.emit(
                events.DataReceived,
                new TextEncoder().encode(
                  JSON.stringify({
                    v: 1,
                    type: 'user_end_ack',
                    request_id: packet.request_id,
                    agent_sid: this.peer.agent?.sid,
                  })
                ),
                this.peer.agent,
                0,
                'xiaoya.delivery'
              )
            );
        },
      };
      this.notify();
      options.configure?.(this);
    }
    /** SDK 的公开状态替换为稳定快照，以真实 useSyncExternalStore 驱动 React。 */
    notify = () => {
      this.snapshot = {
        state: this.state,
        peer: this.peer,
        messages: this.messages,
        sending: this.sending,
        deviceId: this.deviceId,
        track: this.localParticipant?.getTrackPublication()?.track,
      };
      for (const listener of this.listeners) listener();
    };
    /** 订阅由真实 React effect 管理，卸载数量可被断言。 */
    subscribe = (listener) => {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    };
    getSnapshot = () => this.snapshot;
    /** 状态事件允许先于 React commit，测试不能通过手动重新渲染修复所有权。 */
    setState(state) {
      this.state = state;
      this.emit(events.ConnectionStateChanged, state);
      this.notify();
    }
    /** 同一个身份换 SID 用于验证重新授权，不改 Room 对象来隐藏竞态。 */
    setPeer(peer) {
      this.peer = peer;
      this.remoteParticipants.clear();
      if (peer.agent) this.remoteParticipants.set(peer.agent.identity, peer.agent);
      this.notify();
    }
    /** 只模拟外部 SDK 的连接结果，业务确认仍须经过 DeliveryChannel 的 RPC。 */
    start = async () => {
      this.starts++;
      if (this.startGate) await this.startGate.promise;
      this.localParticipant.sid = 'PA_local';
      this.setPeer({ state: 'listening', agent, track: undefined });
      this.setState('connected');
      this.emit(events.Connected);
    };
    /** 关闭可被延迟，便于验证重复结束共用 Promise 而非提前创建新 Room。 */
    end = async () => {
      this.ends++;
      if (this.endGate) await this.endGate.promise;
      await this.disconnect();
    };
    /** SDK 仅持有其公开房间；测试不替生产控制器关闭借出的音频时钟。 */
    disconnect = async () => {
      this.disconnects++;
      this.setState('disconnected');
    };
    getActiveDevice = () => this.deviceId;
    /** 设备成功与失败使用同一个公开方法，选择恢复逻辑保持由生产控件执行。 */
    switchActiveDevice = async (_, id) => {
      this.switched.push(id);
      if (this.switchGate) await this.switchGate.promise;
      if (this.switchFailure && id !== 'device-a') {
        this.localParticipant.getTrackPublication()?.track.stop();
        this.notify();
        throw this.switchFailure;
      }
      if (this.switchResult === false && id !== 'device-a') return false;
      this.deviceId = id;
      this.notify();
      return true;
    };
  }
  class Context extends window.EventTarget {
    state = 'suspended';
    closes = 0;
    currentTime = 0;
    baseLatency = 0;
    outputLatency = 0;
    /** 浏览器构造端口记录数量，不制造输入或输出音频。 */
    constructor() {
      super();
      contexts.push(this);
    }
    /** 保留自动播放可恢复状态，真实 UI 仍通过公开 SDK 能力显示恢复入口。 */
    async resume() {
      this.state = 'running';
      this.dispatchEvent(new Event('statechange'));
    }
    /** 主控制器拥有关闭操作，画面组件重建不能提前触发它。 */
    async close() {
      this.closes++;
      this.state = 'closed';
      this.dispatchEvent(new Event('statechange'));
    }
  }
  globalThis.AudioContext = Context;
  /** hook 替换的只有外部服务观察端口，实际 React 订阅与提交语义继续生效。 */
  function useRoom(room) {
    return React.useSyncExternalStore(room.subscribe, room.getSnapshot, room.getSnapshot);
  }
  const sdk = {
    /** 当前 SDK 会话绑定按 Room 稳定，测试记录是否混入浏览器 Agent 选择。 */
    useSession(_token, config) {
      const snapshot = useRoom(config.room);
      calls.sessionOptions.push(config);
      return React.useMemo(
        () => ({
          room: config.room,
          start: config.room.start,
          end: config.room.end,
          connectionState: snapshot.state,
        }),
        [config.room, snapshot.state]
      );
    },
    /** 实际 Context 必须已提交，否则下面的 SDK hooks 会抛错。 */
    SessionProvider({ session, children }) {
      return React.createElement(Session.Provider, { value: session }, children);
    },
    /** 宿主只挂载一个可听出口，重复出口与卸载泄漏可计数验证。 */
    RoomAudioRenderer() {
      React.useEffect(() => {
        calls.renderers++;
        return () => {
          calls.renderers--;
        };
      }, []);
      return null;
    },
    /** 房间参与者属于外部服务，测试通过稳定公开快照观察它。 */
    useVoiceAssistant() {
      const session = React.useContext(Session);
      const value = useRoom(session.room);
      return {
        ...value.peer,
        audioTrack: value.peer.track ? { publication: { track: value.peer.track } } : undefined,
      };
    },
    /** 初次连接等待保留取消端口，不自行改变生产确认规则。 */
    useAgent() {
      const session = React.useContext(Session);
      return React.useMemo(
        () => ({
          waitUntilConnected: async (signal) => {
            signal.throwIfAborted();
            if (!session.room.peer.agent) throw new Error('No agent');
          },
        }),
        [session.room]
      );
    },
    /** 真实消息与发送失败由 room 端口控制，草稿清空逻辑完全来自真实控件。 */
    useSessionMessages() {
      const session = React.useContext(Session);
      const value = useRoom(session.room);
      const send = React.useCallback(
        async (text) => {
          session.room.sent ??= [];
          session.room.sent.push(text);
          if (session.room.sendGate) await session.room.sendGate.promise;
          if (session.room.sendFailure) throw session.room.sendFailure;
          return {};
        },
        [session.room]
      );
      return { messages: value.messages, send, isSending: value.sending };
    },
    /** 设备状态仅来自 SDK publication，真实控件检查原生 readyState 并处理重试。 */
    useTrackToggle({ room }) {
      const value = useRoom(room);
      return { track: value.track ? { audioTrack: value.track } : undefined, pending: false };
    },
    /** 标签权限不会在测试中偷偷开启采集；页面只在用户按钮操作时申请媒体。 */
    useMediaDeviceSelect({ room }) {
      const value = useRoom(room);
      return {
        activeDeviceId: value.deviceId,
        devices: [
          { deviceId: 'device-a', label: '麦克风 A' },
          { deviceId: 'device-b', label: '麦克风 B' },
        ],
      };
    },
    useEnsureRoom: (room) => room,
    /** 自动播放入口保留原生按钮属性，但不创建第二音频时钟。 */
    useStartAudio({ room, props }) {
      return { mergedProps: { ...props, hidden: room.canPlaybackAudio } };
    },
  };
  return {
    rooms,
    contexts,
    tracks,
    calls,
    sdk,
    agent,
    ports: {
      'livekit-client': {
        Room,
        RemoteAudioTrack: RemoteTrack,
        ConnectionState: states,
        ParticipantKind: { AGENT: 4 },
        DataPacket_Kind: { RELIABLE: 0, LOSSY: 1 },
        RoomEvent: events,
        Track: { Source: { Microphone: 'microphone' } },
        TrackEvent: { Restarted: 'restarted' },
        TokenSource: { endpoint: (endpoint) => ({ endpoint }) },
        /** 授权结果可在卸载后迟到，生产代码必须主动回收而非测试端先停止。 */
        createLocalAudioTrack: async (config) => {
          calls.microphones.push(config);
          if (options.microphone) return options.microphone(config);
          const track = new LocalTrack();
          tracks.push(track);
          return track;
        },
      },
      '@livekit/components-react': sdk,
    },
  };
}
