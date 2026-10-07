'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Room, TokenSource } from 'livekit-client';
import { useSession } from '@livekit/components-react';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { ViewController } from '@/components/app/view-controller';
import { type SessionAudio, createSessionAudio } from '@/lib/avatar/audio-bridge';
import { SessionAudioContext } from '@/lib/avatar/session-audio';

interface AppProps {
  tokenEndpoint: string;
  agentName?: string;
}
/** 房间和分析共享唯一时钟；浏览器资源只在挂载后创建，避免 SSR 和 StrictMode 泄漏。 */
export function App({ tokenEndpoint, agentName }: AppProps) {
  const tokenSource = useMemo(() => TokenSource.endpoint(tokenEndpoint), [tokenEndpoint]);
  const [room, setRoom] = useState(() => new Room());
  const [audio, setAudio] = useState<SessionAudio | null>(null);
  const owned = useRef<{ room: Room; audio: SessionAudio } | null>(null);
  /** 新尝试独立拥有房间和时钟，旧连接结束后的迟到信令不能影响新连接。 */
  const resetRoom = useCallback(() => {
    const previous = owned.current;
    const nextAudio = createSessionAudio();
    const nextRoom = new Room({ webAudioMix: { audioContext: nextAudio.context } });
    owned.current = { room: nextRoom, audio: nextAudio };
    setAudio(nextAudio);
    setRoom(nextRoom);
    if (previous) void previous.audio.close();
  }, []);
  useEffect(() => {
    resetRoom();
    /** 先断开 SDK 再关闭借给播放器的时钟，组件重试不会管理 SDK 音轨。 */
    return () => {
      const previous = owned.current;
      owned.current = null;
      if (previous) void previous.room.disconnect().finally(() => previous.audio.close());
    };
  }, [resetRoom]);
  const session = useSession(tokenSource, {
    room,
    agentName,
    agentConnectTimeoutMilliseconds: 30_000,
  });
  return (
    <SessionAudioContext.Provider value={audio}>
      <AgentSessionProvider session={session}>
        <ViewController resetRoom={resetRoom} />
      </AgentSessionProvider>
    </SessionAudioContext.Provider>
  );
}
