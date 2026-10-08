'use client';

import { useEffect, useRef } from 'react';
import type { Track } from 'livekit-client';
import { type DeliveryGate, type DeliveryMessage } from '@/lib/avatar/delivery';

/** 网络同步由会话唯一通道持有；此 hook 只跟随本地字幕、状态和音轨截断错过的动作。 */
export function useAvatarDelivery(
  gate: { current: DeliveryGate },
  messages: readonly DeliveryMessage[],
  identity: string | undefined,
  state: string,
  track: Track | undefined
) {
  const previousLocal = useRef<string | undefined>(undefined);
  const initialized = useRef(false);
  const previousTrack = useRef<Track | undefined>(undefined);
  /** 新本地发言定义打断边界；重新读取历史只建立锚点，不重新消费旧表现。 */
  useEffect(() => {
    if (identity) gate.current.offer(messages, identity);
    const local = messages.findLast((message) => message.from?.isLocal)?.id;
    if (initialized.current && local && local !== previousLocal.current) gate.current.interrupt();
    previousLocal.current = local;
    initialized.current = true;
  }, [gate, messages, identity]);
  /** 首次音轨是正常接管，只有已有轨道被替换才取消其未完成动作。 */
  useEffect(() => {
    const previous = previousTrack.current;
    previousTrack.current = track;
    if (previous && previous !== track) gate.current.interrupt();
  }, [gate, track]);
  /** 状态离开 speaking 后立即收束表现，不能等待字幕或下一帧才取消。 */
  useEffect(() => {
    if (state !== 'speaking') gate.current.interrupt();
  }, [gate, state]);
  return gate;
}
