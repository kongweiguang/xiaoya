'use client';

import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import {
  type LocalAudioTrack,
  type Room,
  Track,
  TrackEvent,
  createLocalAudioTrack,
} from 'livekit-client';
import { ChevronDown, LoaderCircle, Mic, MicOff, PhoneOff, Send } from 'lucide-react';
import { useMediaDeviceSelect, useTrackToggle } from '@livekit/components-react';
import { Button } from '@/components/ui/button';
import { microphoneError } from '@/hooks/use-conversation';

interface ConversationControlsProps {
  room: Room;
  draft: string;
  setDraft: Dispatch<SetStateAction<string>>;
  ready: boolean;
  sending: boolean;
  send: (message: string) => Promise<unknown>;
  end: () => Promise<void>;
  onMicrophoneChange: (capturing: boolean) => void;
}

/** Publication 未静音不代表仍在采集；设备切换失败可能留下 ended 轨道。 */
function microphoneCapturing(track?: LocalAudioTrack): boolean {
  return Boolean(
    track &&
      !track.isMuted &&
      track.mediaStreamTrack.enabled &&
      track.mediaStreamTrack.readyState === 'live'
  );
}

/** 页面持有草稿以跨故障重试保留；尺寸只写 DOM，发送与采集所有权不因布局变化重建。 */
export function ConversationControls({
  room,
  draft,
  setDraft,
  ready,
  sending,
  send,
  end,
  onMicrophoneChange,
}: ConversationControlsProps) {
  const [error, setError] = useState('');
  const [deviceError, setDeviceError] = useState('');
  const inFlight = useRef(false);
  const composing = useRef(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(true);
  const deviceOperation = useRef(false);
  const [acquiring, setAcquiring] = useState(false);
  const [captureRevision, setCaptureRevision] = useState(0);
  /** 输入高度禁用过渡以即时测量；先回单行才能收缩，长草稿保留滚动而不挤占页面。 */
  const resizeInput = useCallback(() => {
    if (!input.current) return;
    input.current.style.height = '38px';
    input.current.style.height = `${Math.min(100, input.current.scrollHeight)}px`;
  }, []);
  // 内容提交后即测量，不把临时 DOM 尺寸放进 React 状态或重建会话资源。
  useLayoutEffect(resizeInput, [draft, resizeInput]);
  /** 宽度由局部观察器持有；自身高度变化的通知不测量，避免回环和重复订阅。 */
  useEffect(() => {
    const element = input.current;
    if (!element) return;
    let previousWidth = element.clientWidth;
    let frame = 0;
    /** 宽度事件先完成原生换行排版再测量；高度通知不排帧，连续变宽只保留最后一帧。 */
    const observer = new ResizeObserver(() => {
      const width = element.clientWidth;
      if (width === previousWidth) return;
      previousWidth = width;
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(resizeInput);
    });
    observer.observe(element);
    /** 观察器与待测量帧均随控件释放，旧连接不能在结束后再次写入输入元素。 */
    return () => {
      observer.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, [resizeInput]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  /** 设备失败保持文字输入可用，重试由用户主动触发。 */
  const reportDeviceError = useCallback((failure: Error) => {
    if (mounted.current) setDeviceError(microphoneError(failure));
  }, []);
  const mic = useTrackToggle({
    room,
    source: Track.Source.Microphone,
    onDeviceError: reportDeviceError,
  });
  const devices = useMediaDeviceSelect({
    room,
    kind: 'audioinput',
    requestPermissions: false,
    onError: reportDeviceError,
  });
  const captureTrack = mic.track?.audioTrack;
  const microphoneEnabled = microphoneCapturing(captureTrack);
  /** 按钮与页面使用同一真实采集判定；提交前同步派生值，不另存用户想要的开关状态。 */
  useLayoutEffect(() => {
    onMicrophoneChange(microphoneEnabled);
  }, [microphoneEnabled, onMicrophoneChange]);
  /** 每个异步边界重新读取 SDK 可变房间状态，不能把点击时的 connected 当成持续保证。 */
  const sessionConnected = () => mounted.current && room.state === 'connected';
  useEffect(() => {
    // SDK 换轨可以保持 LocalAudioTrack 包装对象不变，必须跟随原生轨道重绑监听器。
    if (!captureTrack) return;
    const media = captureTrack.mediaStreamTrack;
    /** 硬件结束和 SDK 换轨都需重新读取真实采集；不对每帧音量建立 React 状态。 */
    function refreshCapture() {
      // 修订号仅用于重绑新轨道监听器，不保存可能落后于 SDK 的开关状态。
      if (mounted.current) setCaptureRevision((current) => current + 1);
    }
    media.addEventListener('ended', refreshCapture);
    captureTrack.on(TrackEvent.Restarted, refreshCapture);
    return () => {
      // 换轨和卸载都解除同一组监听器，不能仅依靠 mounted 避免旧事件写入。
      media.removeEventListener('ended', refreshCapture);
      captureTrack.off(TrackEvent.Restarted, refreshCapture);
    };
  }, [captureTrack, captureRevision]);
  /** 失败轨道不再占用麦克风 publication，下一次点击可直接重新采集，而非先关再开。 */
  const removeEndedMicrophone = async (): Promise<boolean> => {
    const track = room.localParticipant.getTrackPublication(Track.Source.Microphone)?.audioTrack;
    if (!track || track.mediaStreamTrack.readyState !== 'ended') return true;
    track.stop();
    try {
      await room.localParticipant.unpublishTrack(track);
    } catch {
      // SDK 先移除 publication 再协商；协商失败不能阻止释放已失效的采集。
    }
    return room.localParticipant.getTrackPublication(Track.Source.Microphone)?.audioTrack !== track;
  };
  /** 回车只在输入法完成后发送；发送失败保留原文和焦点方便重试。 */
  const submit = async () => {
    const text = draft.trim();
    if (!text || !ready || sending || inFlight.current) return;
    inFlight.current = true;
    setError('');
    try {
      await send(text);
      // 发送期间仍允许编辑，成功只能清除提交时的那份草稿。
      if (mounted.current) setDraft((current) => (current === draft ? '' : current));
    } catch {
      if (mounted.current) setError('发送失败，内容已保留。请再次点击发送。');
    } finally {
      inFlight.current = false;
      input.current?.focus();
    }
  };
  /** 以实际采集决定开关；失效轨道重新创建，SDK 换轨仍保留其停止后的迟到回收约束。 */
  const toggleMic = async () => {
    if (!ready || deviceOperation.current || !sessionConnected()) return;
    deviceOperation.current = true;
    setDeviceError('');
    setAcquiring(true);
    try {
      const current = room.localParticipant.getTrackPublication(
        Track.Source.Microphone
      )?.audioTrack;
      if (current?.mediaStreamTrack.readyState === 'live') {
        await room.localParticipant.setMicrophoneEnabled(!microphoneCapturing(current));
        if (!mounted.current || room.state === 'disconnected') current.stop();
      } else {
        if (!(await removeEndedMicrophone())) throw new Error('失效麦克风尚未释放');
        if (!mounted.current || room.state !== 'connected') return;
        const track = await createLocalAudioTrack({
          deviceId: room.getActiveDevice('audioinput') || devices.activeDeviceId || undefined,
        });
        if (!mounted.current || room.state !== 'connected') {
          track.stop();
          return;
        }
        try {
          await room.localParticipant.publishTrack(track, { source: Track.Source.Microphone });
          if (!mounted.current || room.state !== 'connected') {
            try {
              await room.localParticipant.unpublishTrack(track);
            } finally {
              track.stop();
            }
          }
        } catch (failure) {
          track.stop();
          throw failure;
        }
      }
    } catch (failure) {
      await removeEndedMicrophone();
      if (mounted.current) setDeviceError(microphoneError(failure));
    } finally {
      deviceOperation.current = false;
      if (mounted.current) setAcquiring(false);
    }
  };
  /** 串行切换避免后一次选择被旧失败覆盖；只用 SDK 公共接口恢复上一设备。 */
  const switchMicrophone = async (deviceId: string) => {
    if (!ready || deviceOperation.current || !sessionConnected()) return;
    deviceOperation.current = true;
    const previousDeviceId =
      room.getActiveDevice('audioinput') || devices.activeDeviceId || 'default';
    setDeviceError('');
    setAcquiring(true);
    try {
      const switched = await room.switchActiveDevice('audioinput', deviceId);
      if (!switched) throw new Error('麦克风设备未切换');
    } catch (failure) {
      const removed = await removeEndedMicrophone();
      if (mounted.current && room.state === 'connected') {
        if (removed) {
          try {
            await room.switchActiveDevice('audioinput', previousDeviceId);
          } catch {
            // 原设备也可能被拔出；保持关闭和显式错误，由用户选择设备后重新开启。
          }
        }
        setDeviceError(microphoneError(failure));
      }
    } finally {
      deviceOperation.current = false;
      if (mounted.current) setAcquiring(false);
    }
  };
  return (
    <div className="control-area">
      {error && (
        <p className="control-error" role="alert">
          {error}
        </p>
      )}
      {deviceError && (
        <p className="control-error" role="alert">
          {deviceError}
        </p>
      )}
      <div className="conversation-controls">
        <div className="microphone-control">
          <Button
            variant="ghost"
            className="mic-button"
            onClick={toggleMic}
            disabled={!ready || mic.pending || acquiring}
            aria-pressed={microphoneEnabled}
            aria-label={microphoneEnabled ? '关闭麦克风' : '开启麦克风'}
            aria-busy={acquiring}
          >
            <span className={microphoneEnabled ? 'mic-icon enabled' : 'mic-icon'}>
              {mic.pending || acquiring ? (
                <LoaderCircle className="spin" />
              ) : microphoneEnabled ? (
                <Mic />
              ) : (
                <MicOff />
              )}
            </span>
            <span>
              {acquiring ? '麦克风准备中…' : microphoneEnabled ? '麦克风已开启' : '麦克风已关闭'}
            </span>
          </Button>
          <details className="device-menu">
            <summary aria-label="选择麦克风" aria-disabled={!ready}>
              <ChevronDown size={18} />
            </summary>
            <div className="device-menu-content">
              <label htmlFor="microphone-device">麦克风设备</label>
              <select
                id="microphone-device"
                value={devices.activeDeviceId}
                disabled={!ready || acquiring}
                onChange={(event) => {
                  // 选择和开关共享互斥边界，切换失败由同一次操作负责恢复及提示。
                  void switchMicrophone(event.target.value);
                }}
              >
                {!devices.devices.some((device) => device.deviceId === devices.activeDeviceId) && (
                  <option value={devices.activeDeviceId}>默认麦克风</option>
                )}
                {devices.devices.map((device, index) => (
                  <option
                    key={device.deviceId || index}
                    value={device.deviceId}
                    disabled={!device.deviceId}
                  >
                    {device.label || `麦克风 ${index + 1}`}
                  </option>
                ))}
              </select>
            </div>
          </details>
        </div>
        <form
          className="chat-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <textarea
            ref={input}
            // 全局减少动效的极短 duration 仍会插值高度；自动输入尺寸必须即时生效。
            style={{ transitionProperty: 'none' }}
            aria-label="聊天内容"
            placeholder={microphoneEnabled ? '想说什么，也可以打字…' : '想聊什么，打字告诉我…'}
            rows={1}
            value={draft}
            disabled={!ready}
            onChange={(event) => setDraft(event.target.value)}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onKeyDown={(event) => {
              if (
                event.key === 'Enter' &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                !composing.current &&
                event.keyCode !== 229
              ) {
                event.preventDefault();
                void submit();
              }
            }}
          />
          <Button
            type="submit"
            size="icon"
            variant="ghost"
            aria-label={error ? '重试发送' : '发送文字'}
            disabled={!ready || sending || !draft.trim()}
          >
            {sending ? <LoaderCircle className="spin" /> : <Send size={21} />}
          </Button>
        </form>
        <Button className="end-button" onClick={() => void end()}>
          <PhoneOff size={19} />
          结束聊天
        </Button>
      </div>
    </div>
  );
}
