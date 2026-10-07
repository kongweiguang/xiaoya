'use client';

import { createContext, useContext } from 'react';
import type { SessionAudio } from './audio-bridge';

export const SessionAudioContext = createContext<SessionAudio | null>(null);

/** 只借用房间拥有的输出时钟，人物组件重试不会更换或关闭正在播放的声音。 */
export function useSessionAudio(): SessionAudio | null {
  return useContext(SessionAudioContext);
}
