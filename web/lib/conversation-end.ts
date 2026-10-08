import {
  ConnectionState,
  DataPacket_Kind,
  ParticipantKind,
  type RemoteParticipant,
  type Room,
  RoomEvent,
} from 'livekit-client';

/** 可靠发送也可能只是排队；同房同 Agent 的确认才证明结束意图到达，最多等待一秒。 */
export function acknowledgeConversationEnd(
  room: Room,
  agent?: RemoteParticipant
): Promise<boolean> {
  if (
    !agent?.sid ||
    agent.kind !== ParticipantKind.AGENT ||
    room.state !== ConnectionState.Connected
  )
    return Promise.resolve(false);
  const identity = agent.identity;
  const sid = agent.sid;
  const requestId = crypto.randomUUID();
  return new Promise((resolve) => {
    let finished = false;
    /** 所有退出共享解除路径；发送卡住、迟到确认和换轨后的旧回调都不能遗留监听。 */
    function finish(acknowledged: boolean) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      room.off(RoomEvent.DataReceived, receive);
      room.off(RoomEvent.ConnectionStateChanged, connectionChanged);
      resolve(acknowledged);
    }
    /** 来源使用 SDK 参与者而非正文身份；旧请求、伪造包和过长数据不能提前结束等待。 */
    function receive(
      payload: Uint8Array,
      participant?: RemoteParticipant,
      kind?: DataPacket_Kind,
      topic?: string
    ) {
      if (
        topic !== 'xiaoya.delivery' ||
        kind !== DataPacket_Kind.RELIABLE ||
        participant?.identity !== identity ||
        participant.sid !== sid ||
        participant.kind !== ParticipantKind.AGENT ||
        payload.byteLength > 512
      )
        return;
      try {
        const packet = JSON.parse(new TextDecoder().decode(payload));
        if (
          packet?.v === 1 &&
          Object.keys(packet).length === 4 &&
          packet.type === 'user_end_ack' &&
          packet.request_id === requestId &&
          packet.agent_sid === sid
        )
          finish(true);
      } catch {
        // 损坏控制包不影响正常聊天，也不能把内部数据带入字幕。
      }
    }
    /** 连接已离开 connected 时不等重放，立即让原 SDK 断开并由后端恢复期限兜底。 */
    function connectionChanged(state: ConnectionState) {
      if (state !== ConnectionState.Connected) finish(false);
    }
    const timer = setTimeout(() => finish(false), 1_000);
    room.on(RoomEvent.DataReceived, receive);
    room.on(RoomEvent.ConnectionStateChanged, connectionChanged);
    try {
      void room.localParticipant
        .publishData(
          new TextEncoder().encode(
            JSON.stringify({
              v: 1,
              type: 'user_end',
              request_id: requestId,
              target_agent_sid: sid,
            })
          ),
          { reliable: true, topic: 'xiaoya.delivery', destinationIdentities: [identity] }
        )
        .catch(() => finish(false));
    } catch {
      finish(false);
    }
  });
}
