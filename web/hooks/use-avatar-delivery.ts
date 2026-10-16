'use client';

import { useEffect, useRef } from 'react';
import type { Track } from 'livekit-client';
import { type DeliveryGate, type DeliveryMessage } from '@/lib/avatar/delivery';

/** 字幕仅提供语义锚点；新输入由真实发送或 VAD 入口取消，迟到本地回显不伤新轮次。 */
export function useAvatarDelivery(
  gate: { current: DeliveryGate },
  messages: readonly DeliveryMessage[],
  identity: string | undefined,
  state: string,
  track: Track | undefined
) {
  const previousTrack = useRef<Track | undefined>(undefined);
  const previousState = useRef(state);
  /** 字幕更新只核对片段，不以迟到回显推断当前服务器轮次已失效。 */
  useEffect(() => {
    if (identity) gate.current.offer(messages, identity);
  }, [gate, messages, identity]);
  /** 首次音轨是正常接管，只有已有轨道被替换才取消其未完成动作。 */
  useEffect(() => {
    const previous = previousTrack.current;
    previousTrack.current = track;
    if (previous && previous !== track) gate.current.interrupt();
  }, [gate, track]);
  /** SDK工具阶段只暂停已播放旧段；仅处理离开speaking边沿，不反复封住提前到达的待播新段。 */
  useEffect(() => {
    const previous = previousState.current;
    previousState.current = state;
    if (previous === 'speaking' && state !== 'speaking') gate.current.suspendSpeaking();
  }, [gate, state]);
  return gate;
}
