'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ConnectionState, RoomEvent, Track, createLocalAudioTrack } from 'livekit-client';
import { useAgent, useSessionContext, useVoiceAssistant } from '@livekit/components-react';
import { useSessionAudio } from '@/lib/avatar/session-audio';

/** 错误只展示可操作的原因，不将令牌或内部服务信息带到界面。 */
export function microphoneError(error: unknown): string {
  const name = error instanceof Error ? error.name : '';
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    return '麦克风权限被拒绝。请允许浏览器使用麦克风，或改用文字聊天。';
  }
  return '麦克风暂不可用。请检查设备后重试，或继续使用文字聊天。';
}

/** 当前参与者的公开状态才证明恢复成功，SDK 历史失败标记不能替代正在工作的 Agent。 */
function assistantAvailable(state: string): boolean {
  return state === 'listening' || state === 'thinking' || state === 'speaking';
}

/** 信令恢复与完整重连共享同一等待窗口，不因 SDK 在两种模式间切换重置超时。 */
function roomRecovering(state: ConnectionState): boolean {
  return state === ConnectionState.Reconnecting || state === ConnectionState.SignalReconnecting;
}

/** 一次尝试覆盖房间连接和 Agent 就绪；取消、超时及迟到结果都受同一代次约束。 */
export function useConversation(resetRoom: () => void) {
  const session = useSessionContext();
  const startupAgent = useAgent();
  const agent = useVoiceAssistant();
  const audio = useSessionAudio();
  const latest = useRef({ session, startupAgent, agentState: agent.state });
  latest.current = { session, startupAgent, agentState: agent.state };
  const attempt = useRef<{
    id: number;
    controller: AbortController;
    timer?: ReturnType<typeof setTimeout>;
  } | null>(null);
  const serial = useRef(0);
  const active = useRef(false);
  const closing = useRef<Promise<void> | null>(null);
  const [phase, setPhase] = useState<'idle' | 'connecting' | 'active' | 'ending'>('idle');
  const [error, setError] = useState('');
  const [microphoneFailed, setMicrophoneFailed] = useState(false);
  const [conversationId, setConversationId] = useState(0);
  const reconnecting =
    phase === 'active' && (roomRecovering(session.room.state) || !assistantAvailable(agent.state));

  /** 共享一次关闭任务，双击与故障回收不能迟到重置后来建立的新房间。 */
  const end = useCallback(() => {
    if (closing.current) return closing.current;
    const closeId = ++serial.current;
    clearTimeout(attempt.current?.timer);
    attempt.current?.controller.abort();
    attempt.current = null;
    active.current = false;
    setPhase('ending');
    const closingSession = latest.current.session;
    /** 无论 SDK 关闭结果如何都回收旧采集，但只有当前代次能改变界面和资源所有权。 */
    async function close() {
      try {
        await closingSession.end();
      } catch {
        if (serial.current === closeId) setError('连接关闭异常，请重新连接。');
      } finally {
        closingSession.room.localParticipant.audioTrackPublications.forEach((publication) => {
          publication.track?.stop();
        });
        if (serial.current === closeId) {
          resetRoom();
          setPhase('idle');
        }
      }
    }
    const pending = close().finally(() => {
      if (closing.current === pending) closing.current = null;
    });
    closing.current = pending;
    return pending;
  }, [resetRoom]);

  /** 禁止重入，等待清理结束才开放下一次连接，避免 SDK 共享房间发生交叉断开。 */
  const start = useCallback(
    async (withMicrophone = true) => {
      if (attempt.current || active.current || closing.current || phase === 'ending') return;
      const id = ++serial.current;
      const controller = new AbortController();
      const startingSession = latest.current.session;
      const startingAgent = latest.current.startupAgent;
      attempt.current = { id, controller };
      setError('');
      setMicrophoneFailed(false);
      setPhase('connecting');
      setConversationId(id);
      // 用户手势尚有效时开启共享时钟；自动播放受阻仍由“开启声音”按钮恢复。
      void audio?.context.resume().catch(() => undefined);
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, 30_000);
      attempt.current.timer = timer;
      /** SDK 在令牌请求或权限等待中可能迟到，旧房间连接成功时立即回收。 */
      const disposeLateConnection = () => {
        if (controller.signal.aborted)
          void startingSession.room.disconnect().catch(() => undefined);
      };
      startingSession.room.on(RoomEvent.Connected, disposeLateConnection);
      let rejectAbort: () => void = () => undefined;
      const aborted = new Promise<never>((_, reject) => {
        rejectAbort = () => reject(new DOMException('连接已取消', 'AbortError'));
        controller.signal.addEventListener('abort', rejectAbort, { once: true });
      });
      /** 权限请求无法由浏览器取消，先持有轨道再发布才能停止取消后的迟到采集。 */
      const connect = async () => {
        const track = withMicrophone
          ? await createLocalAudioTrack().then((track) => {
              if (controller.signal.aborted) {
                track.stop();
                throw new DOMException('麦克风准备已取消', 'AbortError');
              }
              controller.signal.addEventListener('abort', () => track.stop(), { once: true });
              return track;
            })
          : undefined;
        controller.signal.throwIfAborted();
        await startingSession.start({
          signal: controller.signal,
          tracks: { microphone: { enabled: false }, camera: { enabled: false } },
        });
        controller.signal.throwIfAborted();
        if (track)
          await startingSession.room.localParticipant.publishTrack(track, {
            source: Track.Source.Microphone,
          });
        await startingAgent.waitUntilConnected(controller.signal);
      };
      const connectingSession = connect().finally(() => {
        startingSession.room.off(RoomEvent.Connected, disposeLateConnection);
        if (controller.signal.aborted) return startingSession.room.disconnect();
      });
      try {
        await Promise.race([connectingSession, aborted]);
        if (serial.current === id && !controller.signal.aborted) {
          active.current = true;
          setPhase('active');
        }
      } catch (failure) {
        if (serial.current !== id) return;
        controller.abort();
        const name = failure instanceof Error ? failure.name : '';
        const deviceFailure =
          /NotAllowed|NotFound|NotReadable|PermissionDenied|Overconstrained/.test(name);
        setMicrophoneFailed(deviceFailure);
        setError(
          timedOut
            ? '连接超过 30 秒，请检查服务后重试。'
            : deviceFailure
              ? microphoneError(failure)
              : '连接失败，请稍后重试。'
        );
        await startingSession.end().catch(() => undefined);
        // 清理等待期间可能已挂断并建立新会话，旧失败只能回收自身，不能再重置新房间。
        if (serial.current !== id) return;
        resetRoom();
        setPhase('idle');
      } finally {
        clearTimeout(timer);
        controller.signal.removeEventListener('abort', rejectAbort);
        if (attempt.current?.id === id) attempt.current = null;
      }
    },
    [phase, resetRoom, audio]
  );

  /** 完整重连会暂时移除所有远端参与者；仅最终房间断开立即结束，Agent 恢复交给独立窗口。 */
  useEffect(() => {
    if (!active.current || phase !== 'active') return;
    if (session.room.state === ConnectionState.Disconnected) {
      setError('聊天连接已断开，可以重新连接。');
      void end();
    }
  }, [session.connectionState, session.room, session.room.state, phase, end]);

  /** 同一窗口覆盖网络与 Agent 恢复，迟到计时器必须核对代次和当前状态再结束真实会话。 */
  useEffect(() => {
    if (!reconnecting || phase !== 'active' || !active.current) return;
    const recoveryId = serial.current;
    const recoveringRoom = session.room;
    /** SDK 事件可能先于 React effect 清理到达，恢复后的公开状态能阻止已排队的超时误挂断。 */
    function recoveryTimeout() {
      const current = latest.current;
      if (
        !active.current ||
        serial.current !== recoveryId ||
        current.session.room !== recoveringRoom ||
        (!roomRecovering(current.session.room.state) && assistantAvailable(current.agentState))
      )
        return;
      setError('恢复连接超时，请重新连接。');
      void end();
    }
    const timer = setTimeout(recoveryTimeout, 30_000);
    return () => clearTimeout(timer);
  }, [reconnecting, phase, end, session.room]);

  useEffect(
    () => () => {
      ++serial.current;
      clearTimeout(attempt.current?.timer);
      attempt.current?.controller.abort();
      void latest.current.session.end().catch(() => undefined);
    },
    []
  );

  return { phase, error, microphoneFailed, conversationId, reconnecting, start, end, agent };
}
