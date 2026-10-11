'use client';

import { useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import { RemoteAudioTrack, type Room, RoomEvent } from 'livekit-client';
import { Button } from '@/components/ui/button';
import { useAvatarDelivery } from '@/hooks/use-avatar-delivery';
import { useReducedMotionPreference } from '@/hooks/use-reduced-motion-preference';
import {
  AudioEnvelopeTimeline,
  AvatarAudioBridge,
  type SessionAudio,
} from '@/lib/avatar/audio-bridge';
import { selectBehavior } from '@/lib/avatar/behavior';
import type { DeliveryGate, DeliveryMessage } from '@/lib/avatar/delivery';
import { LipSyncTimeline, getAudibleAudioTime } from '@/lib/avatar/lip-sync';
import type { AvatarFrame, Live2DRuntime } from '@/lib/avatar/live2d-runtime';
import type { MotionSyncAnalyzer } from '@/lib/avatar/motion-sync';
import type { ConversationPeer } from '@/lib/conversation-controller';
import avatarPoster from '@/public/avatar/xiaoya/concept-v1/poster.png';

interface AvatarStageProps {
  room?: Room;
  audio: SessionAudio | null;
  peer: ConversationPeer;
  delivery: { current: DeliveryGate };
  connected: boolean;
  reconnecting: boolean;
  status: string;
  error?: boolean;
  messages?: readonly DeliveryMessage[];
}
const EMPTY_MESSAGES: readonly DeliveryMessage[] = [];

/** React 只管理加载和恢复；后备图取自同一原画模型的静音帧，加载与降级不切换角色长相。 */
export function AvatarStage({
  room,
  audio,
  peer,
  delivery: deliveryGate,
  connected,
  reconnecting,
  status,
  error = false,
  messages = EMPTY_MESSAGES,
}: AvatarStageProps) {
  const { state, agent, track } = peer;
  const reducedMotion = useReducedMotionPreference();
  const previousReducedMotion = useRef(reducedMotion);
  const delivery = useAvatarDelivery(deliveryGate, messages, agent?.identity, state, track);
  const canvas = useRef<HTMLCanvasElement>(null);
  const runtime = useRef<Live2DRuntime | null>(null);
  const [timeline] = useState(() => new LipSyncTimeline());
  const [envelopeTimeline] = useState(() => new AudioEnvelopeTimeline());
  const latest = useRef({
    audio,
    room,
    connected,
    reconnecting,
    state,
    error,
    reducedMotion,
    track,
  });
  latest.current = {
    audio,
    room,
    connected,
    reconnecting,
    state,
    error,
    reducedMotion,
    track,
  };
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [analysisMode, setAnalysisMode] = useState('loading');
  const [retry, setRetry] = useState(0);
  const behavior = selectBehavior({ connected, reconnecting, error, agent: state });

  /** GPU 仅随动画偏好或显式重试重建，房间和模型的生命周期互不误伤。 */
  useEffect(() => {
    // 静态期间的新回复也已错过动作时机；恢复时取消它，但首次正常加载不取消开场。
    const returningFromStatic = previousReducedMotion.current && !reducedMotion;
    previousReducedMotion.current = reducedMotion;
    setReady(false);
    setFailed(false);
    timeline.clear();
    envelopeTimeline.clear();
    if (reducedMotion || returningFromStatic) delivery.current.interrupt();
    if (reducedMotion) {
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
    let playbackWasAvailable = false;
    /** 口型和动作共用播放身份栅栏；迟到 PCM 不能在倾听、取消或恢复后独自重新授权嘴部。 */
    function readPerformance() {
      const current = latest.current;
      const visualAvailable = Boolean(
        current.connected &&
          !current.reconnecting &&
          !current.error &&
          !current.reducedMotion &&
          !document.hidden
      );
      const available = Boolean(
        visualAvailable &&
          current.state === 'speaking' &&
          current.room?.canPlaybackAudio &&
          current.audio?.context.state === 'running' &&
          current.track &&
          !current.track.isMuted &&
          current.track.mediaStreamTrack.readyState !== 'ended'
      );
      // 停止期间到达的尾窗也丢弃，下一回复不能借用取消回复的最近开口帧。
      if (!available) {
        timeline.clear();
        envelopeTimeline.clear();
        // 只有已经实际接管的回复失去许可才取消，初始化不能误封尚未出声的开场。
        if (playbackWasAvailable) delivery.current.interruptSpeaking();
      }
      const at = current.audio ? getAudibleAudioTime(current.audio.context) : 0;
      const lip = timeline.select(at);
      const envelope = envelopeTimeline.select(at);
      const selected = delivery.current.read(
        available,
        envelope.energy > 0.025 || lip.open > 0.015,
        visualAvailable
      );
      // 取消后清尾窗；已绑定字幕的新候选仍须保留未来帧，等设备输出而不形成首声死锁。
      if (selected?.phase !== 'speaking' && !delivery.current.awaitingAudibleSpeech) {
        timeline.clear();
        envelopeTimeline.clear();
      }
      playbackWasAvailable = available && selected?.phase === 'speaking';
      return { available, lip, envelope, selected };
    }
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
            /** 实际音频与当前回复授权缺一即闭嘴，停止后的下一渲染帧不接受旧尾窗。 */
            readLip() {
              const frame = readPerformance();
              return frame.available && frame.selected?.phase === 'speaking' ? frame.lip : null;
            },
            /** 同步字幕只提供语义锚点，首次手势仍等待用户真正可听的音频窗口。 */
            readDelivery() {
              const { selected, envelope } = readPerformance();
              activeDelivery = selected ? { ...selected, audio: envelope } : null;
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
        controller.abort();
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
      envelopeTimeline.clear();
    };
  }, [reducedMotion, retry, timeline, envelopeTimeline, delivery]);

  /** 生命周期姿态与语义表情分离，SDK 更新只改变轻微姿态，不生成情绪决策。 */
  useEffect(() => {
    runtime.current?.setBehavior(behavior);
  }, [behavior, ready]);

  /** 同一个已发布音轨借用会话时钟派生口型与重音，取消时一起清空对应时间线。 */
  useEffect(() => {
    timeline.clear();
    envelopeTimeline.clear();
    if (
      !connected ||
      reconnecting ||
      !ready ||
      !audio ||
      !room ||
      !(track instanceof RemoteAudioTrack) ||
      !runtime.current
    )
      return;
    const controller = new AbortController();
    const instance = runtime.current;
    const boundRoom = room;
    const boundTrack = track;
    let bridge: AvatarAudioBridge | undefined;
    let analyzer: MotionSyncAnalyzer | undefined;
    let wasPlaybackUnavailable = false;
    /** 实际播放许可变化立即门控采样，受阻时不张嘴、不增加第二路声音。 */
    function playback() {
      bridge?.setPlaybackAvailable(boundRoom.canPlaybackAudio);
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
          /** 音量与起音来自同一 PCM 窗口；按实际输出时钟取帧，避免字幕先到就发力。 */
          onEnvelope(frame) {
            if (!controller.signal.aborted) envelopeTimeline.push(frame);
          },
          /** 真实能力丢失与恢复都取消当下回复；首次正常清嘴和重复事件不消耗开场许可。 */
          onSilent() {
            if (controller.signal.aborted) return;
            timeline.clear();
            envelopeTimeline.clear();
            analyzer?.reset();
            const unavailable =
              audio!.context.state !== 'running' ||
              !boundRoom.canPlaybackAudio ||
              boundTrack.isMuted ||
              boundTrack.mediaStreamTrack.readyState === 'ended';
            if (unavailable === wasPlaybackUnavailable) return;
            wasPlaybackUnavailable = unavailable;
            if (unavailable) delivery.current.invalidatePlayback();
            else delivery.current.interruptSpeaking();
          },
          /** 分析故障只停止口型并提供重试，不结束当前语音会话。 */
          onError() {
            timeline.clear();
            envelopeTimeline.clear();
            delivery.current.interrupt();
            setAnalysisMode('unavailable');
          },
        });
        playback();
        boundRoom.on(RoomEvent.AudioPlaybackStatusChanged, playback);
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
      boundRoom.off(RoomEvent.AudioPlaybackStatusChanged, playback);
      bridge?.dispose();
      analyzer?.dispose();
      timeline.clear();
      envelopeTimeline.clear();
    };
  }, [
    audio,
    track,
    connected,
    reconnecting,
    ready,
    room,
    retry,
    timeline,
    envelopeTimeline,
    delivery,
  ]);

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
          width={1254}
          height={1254}
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
