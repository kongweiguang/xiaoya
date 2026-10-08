'use client';

import { useEffect, useLayoutEffect, useMemo } from 'react';
import { TokenSource } from 'livekit-client';
import {
  type UseSessionReturn,
  useAgent,
  useSession,
  useSessionMessages,
  useVoiceAssistant,
} from '@livekit/components-react';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { ViewController } from '@/components/app/view-controller';
import { useConversation } from '@/hooks/use-conversation';
import type { ConversationAttempt } from '@/lib/conversation-controller';

/** 会话观察器仅存在于真实尝试内，SDK 上下文提交后再启动，避免连接使用旧占位房间。 */
function SessionObserver({
  attempt,
  session,
}: {
  attempt: ConversationAttempt;
  session: UseSessionReturn;
}) {
  const agent = useVoiceAssistant();
  const startup = useAgent();
  const { messages, send, isSending } = useSessionMessages();
  const track = agent.audioTrack?.publication.track;
  /** 公开 SID 即使在同一个 SDK 包装对象内变化，也必须在用户交互前重新核对许可。 */
  useLayoutEffect(() => {
    attempt.observe({ state: agent.state, agent: agent.agent, track }, messages, isSending, send);
  }, [
    attempt,
    agent.state,
    agent.agent,
    agent.agent?.sid,
    agent.agent?.identity,
    agent.agent?.kind,
    attempt.room.localParticipant.sid,
    track,
    messages,
    isSending,
    send,
    session.connectionState,
  ]);
  /** 先完成整个 SDK 上下文的提交，再启动连接，避免初次事件落在尚未订阅的组件。 */
  useEffect(() => {
    // SDK 的父级被动订阅也要完成提交；控制器确保 StrictMode 不重复发起本次连接。
    queueMicrotask(() => attempt.connect(session, startup));
  }, [attempt, session, startup]);
  return null;
}

/** 每次尝试独立绑定 useSession；唯一音频出口随其结束卸载，页面与 GPU 舞台保持挂载。 */
function SessionHost({
  attempt,
  tokenEndpoint,
}: {
  attempt: ConversationAttempt;
  tokenEndpoint: string;
}) {
  const tokenSource = useMemo(() => TokenSource.endpoint(tokenEndpoint), [tokenEndpoint]);
  const session = useSession(tokenSource, {
    room: attempt.room,
    agentConnectTimeoutMilliseconds: 30_000,
  });
  return (
    <AgentSessionProvider session={session}>
      <SessionObserver attempt={attempt} session={session} />
    </AgentSessionProvider>
  );
}

/** 页面内存与连接资源分离，故障重连保留草稿、历史、角色及用户选择的主题。 */
export function App({ tokenEndpoint }: { tokenEndpoint: string }) {
  const conversation = useConversation();
  return (
    <>
      <ViewController conversation={conversation} />
      {conversation.attempt && (
        <SessionHost
          key={conversation.attempt.id}
          attempt={conversation.attempt}
          tokenEndpoint={tokenEndpoint}
        />
      )}
    </>
  );
}
