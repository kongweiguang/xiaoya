'use client';

import { useEffect, useRef } from 'react';
import { ConnectionState, type RemoteParticipant, RoomEvent, RpcError } from 'livekit-client';
import { useSessionContext, useVoiceAssistant } from '@livekit/components-react';
import {
  DeliveryGate,
  type DeliveryMessage,
  type DeliveryState,
  parseDelivery,
} from '@/lib/avatar/delivery';

/** 协议仅更新 ref，逐帧读取不会把语音频率带进 React；所有响应绑定本次连接代次。 */
export function useAvatarDelivery(
  messages: readonly DeliveryMessage[],
  connected: boolean,
  reconnecting: boolean
) {
  const { room } = useSessionContext();
  const { agent, state, audioTrack } = useVoiceAssistant();
  const gate = useRef(new DeliveryGate());
  const latest = useRef({ messages, state });
  latest.current = { messages, state };
  const agentIdentity = agent?.identity;
  const lastLocal = useRef<string | undefined>(undefined);
  const localInitialized = useRef(false);

  useEffect(() => {
    let active = true;
    const cancellation = new AbortController();
    const controller = new DeliveryGate();
    gate.current = controller;
    if (!connected || reconnecting || !agentIdentity || room.state !== ConnectionState.Connected)
      return;
    /** 真实 SDK participant 身份才可信，其他参与者不能伪造人物控制数据。 */
    function receive(
      payload: Uint8Array,
      participant?: RemoteParticipant,
      _kind?: unknown,
      topic?: string
    ) {
      if (!active || topic !== 'xiaoya.delivery' || participant?.identity !== agentIdentity) return;
      const parsed = parseDelivery(new TextDecoder().decode(payload));
      if (parsed) {
        controller.receive(parsed);
        controller.offer(latest.current.messages, agentIdentity!);
      }
    }
    /** 信令变化先锁住旧状态，后续恢复由新 effect 的快照建立新门控。 */
    function connectionChanged(next: ConnectionState) {
      if (next !== ConnectionState.Connected) controller.interrupt();
    }
    /** 浏览器知道用户已开口时先停止动作，不必等待服务端完成识别。 */
    function activeSpeakers() {
      if (room.localParticipant.isSpeaking) controller.interrupt();
    }
    /** 播放许可丢失不在恢复时补演当前句段，只接受后续完整回复。 */
    function playbackChanged() {
      if (!room.canPlaybackAudio) controller.interrupt();
    }
    /** 后台暂停不能在恢复后继续执行过去已准备的手势。 */
    function visibilityChanged() {
      if (document.hidden) controller.interrupt();
    }
    room.on(RoomEvent.DataReceived, receive);
    room.on(RoomEvent.ConnectionStateChanged, connectionChanged);
    room.on(RoomEvent.ActiveSpeakersChanged, activeSpeakers);
    room.on(RoomEvent.AudioPlaybackStatusChanged, playbackChanged);
    document.addEventListener('visibilitychange', visibilityChanged);
    /** 订阅先建立再取快照，迟到 RPC 只能交给其原连接的控制器。 */
    async function synchronize() {
      for (let attempt = 0; attempt < 5 && active; attempt++) {
        try {
          const response = await room.localParticipant.performRpc({
            destinationIdentity: agentIdentity!,
            method: 'xiaoya.getDeliverySnapshot',
            payload: '',
            responseTimeout: 3_000,
          });
          if (!active) return;
          const snapshot: DeliveryState | null = parseDelivery(response);
          if (snapshot) {
            controller.snapshot(snapshot);
            controller.offer(latest.current.messages, agentIdentity!);
          }
          return;
        } catch (error) {
          if (process.env.NODE_ENV === 'development')
            console.debug('xiaoya.delivery snapshot', {
              attempt,
              code: error instanceof RpcError ? error.code : 'unavailable',
            });
          // Agent 已进房不代表 on_enter 已装好 RPC；有界重试，普通 Agent 仍保持中性。
          if (attempt < 4 && active) await retrySnapshot(cancellation.signal, 150 * (attempt + 1));
        }
      }
    }
    void synchronize();
    /** 连接及 Agent 所有权变化解除同一组事件，不让旧回调进入新的控制器。 */
    return () => {
      active = false;
      cancellation.abort();
      controller.interrupt();
      room.off(RoomEvent.DataReceived, receive);
      room.off(RoomEvent.ConnectionStateChanged, connectionChanged);
      room.off(RoomEvent.ActiveSpeakersChanged, activeSpeakers);
      room.off(RoomEvent.AudioPlaybackStatusChanged, playbackChanged);
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [room, agentIdentity, agent, connected, reconnecting]);

  useEffect(() => {
    if (agentIdentity) gate.current.offer(messages, agentIdentity);
    const local = messages.findLast((message) => message.from?.isLocal)?.id;
    if (localInitialized.current && local && local !== lastLocal.current) gate.current.interrupt();
    lastLocal.current = local;
    localInitialized.current = true;
  }, [messages, agentIdentity]);

  useEffect(() => {
    // 首轨到达是初始化而非打断；真正旧轨的清理只取消其原控制器。
    if (!audioTrack?.publication.track) return;
    const controller = gate.current;
    return () => controller.interrupt();
  }, [audioTrack?.publication.track]);

  useEffect(() => {
    // speaking 离开时同步关闭动作；进入 speaking 不会解除被打断的旧句段。
    if (state !== 'speaking') gate.current.interrupt();
  }, [state]);
  return gate;
}

/** 快照重试属于当前连接，卸载立即结束等待，不留下后台轮询或悬挂定时器。 */
function retrySnapshot(signal: AbortSignal, delay: number): Promise<void> {
  return new Promise((resolve) => {
    /** 取消和到期共享解除路径，信号监听不会随重试次数累积。 */
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    }
    const timer = setTimeout(finish, delay);
    signal.addEventListener('abort', finish, { once: true });
    if (signal.aborted) finish();
  });
}
