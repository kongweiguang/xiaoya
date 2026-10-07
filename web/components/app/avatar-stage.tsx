'use client';

import { useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import { RemoteAudioTrack, RoomEvent } from 'livekit-client';
import { useSessionContext, useVoiceAssistant } from '@livekit/components-react';
import { Button } from '@/components/ui/button';
import { useAvatarDelivery } from '@/hooks/use-avatar-delivery';
import { useReducedMotionPreference } from '@/hooks/use-reduced-motion-preference';
import { AvatarAudioBridge } from '@/lib/avatar/audio-bridge';
import { selectBehavior } from '@/lib/avatar/behavior';
import type { DeliveryMessage } from '@/lib/avatar/delivery';
import { LipSyncTimeline, getAudibleAudioTime } from '@/lib/avatar/lip-sync';
import type { AvatarFrame, Live2DRuntime } from '@/lib/avatar/live2d-runtime';
import type { MotionSyncAnalyzer } from '@/lib/avatar/motion-sync';
import { useSessionAudio } from '@/lib/avatar/session-audio';
import avatarPoster from '@/public/avatar/xiaoya/poster.png';

interface AvatarStageProps {
  connected: boolean;
  reconnecting: boolean;
  status: string;
  error?: boolean;
  messages?: readonly DeliveryMessage[];
}
const EMPTY_MESSAGES: readonly DeliveryMessage[] = [];

/** React 只管理加载和恢复；静态图随内容生成地址，修图后不沿用旧缓存，动画故障不影响聊天。 */
export function AvatarStage({
  connected,
  reconnecting,
  status,
  error = false,
  messages = EMPTY_MESSAGES,
}: AvatarStageProps) {
  const { audioTrack, state } = useVoiceAssistant();
  const { room } = useSessionContext();
  const audio = useSessionAudio();
  const reducedMotion = useReducedMotionPreference();
  const delivery = useAvatarDelivery(messages, connected, reconnecting);
  const canvas = useRef<HTMLCanvasElement>(null);
  const runtime = useRef<Live2DRuntime | null>(null);
  const [timeline] = useState(() => new LipSyncTimeline());
  const latest = useRef({
    audio,
    connected,
    reconnecting,
    state,
    error,
    reducedMotion,
    track: audioTrack?.publication.track,
  });
  latest.current = {
    audio,
    connected,
    reconnecting,
    state,
    error,
    reducedMotion,
    track: audioTrack?.publication.track,
  };
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [analysisMode, setAnalysisMode] = useState('loading');
  const [retry, setRetry] = useState(0);
  const behavior = selectBehavior({ connected, reconnecting, error, agent: state });
  const track = audioTrack?.publication.track;

  useEffect(() => {
    setReady(false);
    setFailed(false);
    timeline.clear();
    if (reducedMotion) {
      delivery.current.interrupt();
      return;
    }
    if (!canvas.current) return;
    const deliveryController = delivery.current;
    const controller = new AbortController();
    const node = canvas.current;
    let instance: Live2DRuntime | undefined;
    let timedOut = false;
    let active = true;
    let activeDelivery: ReturnType<(typeof delivery.current)['read']> = null;
    let renderObservation: (AvatarFrame & { wall: number; count: number }) | null = null;
    let rendered = 0;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 20_000);
    /** 晚到加载只回收，不允许旧模型重新接管舞台。 */
    async function load() {
      try {
        const { Live2DRuntime } = await import('@/lib/avatar/live2d-runtime');
        controller.signal.throwIfAborted();
        instance = await Live2DRuntime.load(
          node,
          {
            /** 只读开发诊断保存最后一帧，不触发 React，也不记录实际音频或用户文本。 */
            onFrame(frame) {
              if (process.env.NODE_ENV === 'development')
                renderObservation = { ...frame, wall: performance.now(), count: ++rendered };
            },
            /** 只按实际可听时间取帧，字幕和 speaking 都不能令嘴部空动。 */
            readLip() {
              const current = latest.current;
              if (
                !current.connected ||
                current.reconnecting ||
                !current.audio ||
                current.audio.context.state !== 'running'
              )
                return null;
              return timeline.select(getAudibleAudioTime(current.audio.context));
            },
            /** 同步字幕只提供语义锚点，首次手势仍等待用户真正可听的音频窗口。 */
            readDelivery() {
              const current = latest.current;
              const available = Boolean(
                current.connected &&
                  !current.reconnecting &&
                  !current.error &&
                  !current.reducedMotion &&
                  current.state === 'speaking' &&
                  !document.hidden &&
                  room.canPlaybackAudio &&
                  current.audio?.context.state === 'running' &&
                  current.track &&
                  !current.track.isMuted
              );
              const lip = current.audio
                ? timeline.select(getAudibleAudioTime(current.audio.context))
                : null;
              activeDelivery = delivery.current.read(available, (lip?.open ?? 0) > 0.015);
              return activeDelivery;
            },
            /** GL 丢失保留 canvas 等待恢复，语音和文字继续可用。 */
            onFault() {
              if (!controller.signal.aborted) {
                delivery.current.interrupt();
                setFailed(true);
                setReady(false);
              }
            },
            /** 恢复后重新装载纹理，避免沿用失效的 GPU 句柄。 */
            onRestore() {
              if (!controller.signal.aborted) setRetry((value) => value + 1);
            },
          },
          controller.signal
        );
        if (controller.signal.aborted) {
          instance.dispose();
          return;
        }
        runtime.current = instance;
        if (process.env.NODE_ENV === 'development') {
          Object.defineProperty(window, '__xiaoyaAvatar', {
            configurable: true,
            get: () => runtime.current?.diagnostics,
          });
          Object.defineProperty(window, '__xiaoyaDelivery', {
            configurable: true,
            get: () => activeDelivery,
          });
          Object.defineProperty(window, '__xiaoyaDeliveryState', {
            configurable: true,
            get: () => delivery.current.diagnostics,
          });
          Object.defineProperty(window, '__xiaoyaAudioFrame', {
            configurable: true,
            get: () => renderObservation,
          });
        }
        setReady(true);
      } catch {
        // 超时仍提示恢复，但被重试或卸载替代的加载不能再覆盖新舞台。
        if (active && (!controller.signal.aborted || timedOut)) setFailed(true);
      } finally {
        clearTimeout(timeout);
      }
    }
    void load();
    return () => {
      active = false;
      deliveryController.interrupt();
      controller.abort();
      clearTimeout(timeout);
      instance?.dispose();
      if (runtime.current === instance) runtime.current = null;
      timeline.clear();
    };
  }, [reducedMotion, retry, timeline, room, delivery]);

  useEffect(() => {
    runtime.current?.setBehavior(behavior);
  }, [behavior, ready]);

  useEffect(() => {
    timeline.clear();
    if (
      !connected ||
      reconnecting ||
      !ready ||
      !audio ||
      !(track instanceof RemoteAudioTrack) ||
      !runtime.current
    )
      return;
    const controller = new AbortController();
    const instance = runtime.current;
    const boundTrack = track;
    let bridge: AvatarAudioBridge | undefined;
    let analyzer: MotionSyncAnalyzer | undefined;
    /** 实际播放许可变化立即门控采样，受阻时不张嘴、不增加第二路声音。 */
    function playback() {
      bridge?.setPlaybackAvailable(room.canPlaybackAudio);
    }
    /** 每个音轨独立隔离分析代次，换轨和旧会话不能写入新的时间线。 */
    async function attach() {
      try {
        const { loadSdkScript } = await import('@/lib/avatar/sdk-loader');
        controller.signal.throwIfAborted();
        // Core失效时让分析工厂显式进入音量后备，仍为同一真实音轨建立静音分析支路。
        await loadSdkScript('/avatar/vendor/live2dcubismmotionsynccore.min.js').catch(
          () => undefined
        );
        controller.signal.throwIfAborted();
        const { createMotionSyncAnalyzer } = await import('@/lib/avatar/motion-sync');
        controller.signal.throwIfAborted();
        if (!instance.motionSyncBuffer) throw new Error('模型没有口型配置');
        analyzer = await createMotionSyncAnalyzer(instance.model, instance.motionSyncBuffer, {
          signal: controller.signal,
          /** 旧分析初始化不得改变新会话的降级提示。 */
          onModeChange: (mode) => {
            if (!controller.signal.aborted) setAnalysisMode(mode);
          },
        });
        if (controller.signal.aborted) {
          analyzer.dispose();
          return;
        }
        setAnalysisMode(analyzer.mode);
        bridge = new AvatarAudioBridge({
          context: audio!.context,
          /** 原生块可能比 PCM 窗口长，逐帧入队而不重复最近结果。 */
          onSamples(samples, rate, at) {
            if (!controller.signal.aborted)
              for (const frame of analyzer!.sample(samples, rate, at)) timeline.push(frame);
          },
          /** 静音、受阻、换轨和结束都清除缓存，防止残留张嘴。 */
          onSilent() {
            timeline.clear();
            analyzer?.reset();
            // 初始化分析也会清嘴，只有真实播放能力丢失才取消当前回复的表现。
            if (
              audio!.context.state !== 'running' ||
              !room.canPlaybackAudio ||
              boundTrack.isMuted ||
              boundTrack.mediaStreamTrack.readyState === 'ended'
            )
              delivery.current.interrupt();
          },
          /** 分析故障只停止口型并提供重试，不结束当前语音会话。 */
          onError() {
            timeline.clear();
            delivery.current.interrupt();
            setAnalysisMode('unavailable');
          },
        });
        playback();
        room.on(RoomEvent.AudioPlaybackStatusChanged, playback);
        await bridge.attach(track as RemoteAudioTrack, controller.signal);
      } catch {
        if (!controller.signal.aborted) {
          timeline.clear();
          setAnalysisMode('unavailable');
        }
      }
    }
    void attach();
    return () => {
      controller.abort();
      room.off(RoomEvent.AudioPlaybackStatusChanged, playback);
      bridge?.dispose();
      analyzer?.dispose();
      timeline.clear();
    };
  }, [audio, track, connected, reconnecting, ready, room, retry, timeline, delivery]);

  const animated = ready && !failed && !reducedMotion;
  const audioFailed = connected && ['amplitude', 'unavailable'].includes(analysisMode);
  /** 人物重建只取消当前控制器，跨重连后不能依赖旧加载 effect 捕获的门控对象。 */
  function retryAnimation() {
    delivery.current.interrupt();
    setAnalysisMode('loading');
    setRetry((value) => value + 1);
  }
  return (
    <section className="avatar-panel" aria-label="小芽数字人">
      <div
        className="avatar-media"
        data-renderer={animated ? 'live2d' : 'static'}
        data-lip-sync={analysisMode}
      >
        <Image
          src={avatarPoster}
          alt="奶白色身体、薄荷绿芽叶的机器人小芽"
          width={1280}
          height={1280}
          priority
          className={animated ? 'avatar-poster invisible' : 'avatar-poster'}
        />
        {!reducedMotion && (
          <canvas
            key={retry}
            ref={canvas}
            aria-hidden="true"
            className={animated ? 'avatar-canvas' : 'avatar-canvas invisible'}
          />
        )}
      </div>
      <h1>小芽</h1>
      <p className="avatar-status" role="status">
        <span className="status-dot" />
        {status}
      </p>
      <div className="avatar-note" role="status">
        {reducedMotion ? (
          '已按你的偏好使用静态形象'
        ) : failed || audioFailed ? (
          <>
            <span>{failed ? '动画暂不可用，聊天可继续' : '口型暂不可用，聊天可继续'}</span>
            <Button variant="ghost" size="sm" onClick={retryAnimation}>
              重试动画
            </Button>
          </>
        ) : !ready ? (
          '小芽正在准备…'
        ) : null}
      </div>
    </section>
  );
}
