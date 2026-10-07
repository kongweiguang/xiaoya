import type { RemoteAudioTrack } from 'livekit-client';

export type SessionAudio = {
  context: AudioContext;
  close: () => Promise<void>;
};

export type AvatarAudioBridgeOptions = {
  context: AudioContext;
  onSamples: (samples: Float32Array, sampleRate: number, at: number) => void;
  onSilent: () => void;
  onError?: (error: Error) => void;
};

type Attachment = {
  controller: AbortController;
  cleanups: (() => void)[];
  source?: MediaStreamAudioSourceNode;
  worklet?: AudioWorkletNode;
  gain?: GainNode;
};

const workletModules = new WeakMap<AudioContext, Promise<void>>();

/** 会话拥有时钟；人物桥接只借用它，挂断时由会话集中释放以免切断仍在播放的 SDK。 */
export function createSessionAudio(): SessionAudio {
  const context = new AudioContext({ latencyHint: 'interactive' });
  let closing: Promise<void> | undefined;
  return {
    context,
    /** 重复挂断与卸载共享同一次关闭，避免浏览器抛出已经关闭的上下文错误。 */
    close() {
      closing ??= context.state === 'closed' ? Promise.resolve() : context.close();
      return closing;
    },
  };
}

/** 同一时钟只注册一次处理器，失败则允许明确重试；并行 attach 不重复注册名称。 */
function loadWorklet(context: AudioContext): Promise<void> {
  const cached = workletModules.get(context);
  if (cached) return cached;
  if (!context.audioWorklet) {
    return Promise.reject(new Error('当前浏览器无法使用音频分析，请使用安全连接和支持的浏览器'));
  }
  const pending = context.audioWorklet.addModule('/avatar/pcm-worklet.js');
  workletModules.set(context, pending);
  /** 模块网络失败不锁死该会话的重试，且不产生无人处理的派生拒绝。 */
  void pending.catch(() => {
    if (workletModules.get(context) === pending) workletModules.delete(context);
  });
  return pending;
}

/** addModule 没有取消参数，单独取消等待并在晚到结果后检查代次，避免复活旧音轨。 */
function waitForModule(pending: Promise<void>, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException('音频分析已取消', 'AbortError'));
  /** 两条完成路径都移除取消监听，不让长会话累积 AbortSignal 的回调。 */
  return new Promise((resolve, reject) => {
    /** 等待取消立即结束，底层加载仍由共享 Promise 接收结果。 */
    const abort = () => {
      signal.removeEventListener('abort', abort);
      reject(new DOMException('音频分析已取消', 'AbortError'));
    };
    signal.addEventListener('abort', abort, { once: true });
    /** 正常加载后解除等待监听，附件的独立取消监听继续管理节点生命周期。 */
    const loaded = () => {
      signal.removeEventListener('abort', abort);
      resolve();
    };
    /** 网络或处理器语法错误向调用方传播，由页面决定是否重试。 */
    const failed = (error: unknown) => {
      signal.removeEventListener('abort', abort);
      reject(error);
    };
    void pending.then(loaded, failed);
  });
}

export class AvatarAudioBridge {
  private readonly context: AudioContext;
  private readonly onSamples: AvatarAudioBridgeOptions['onSamples'];
  private readonly onSilent: AvatarAudioBridgeOptions['onSilent'];
  private readonly onError: AvatarAudioBridgeOptions['onError'];
  private attachment: Attachment | null = null;
  private disposed = false;
  private playbackAvailable = false;
  private availableSince = Infinity;

  /** 分析与实际播放共享上下文，只维护静音支路，不创建额外的媒体播放元素。 */
  constructor(options: AvatarAudioBridgeOptions) {
    this.context = options.context;
    this.onSamples = options.onSamples;
    this.onSilent = options.onSilent;
    this.onError = options.onError;
    this.context.addEventListener('statechange', this.handleContextState);
  }

  /** 每次绑定用独立对象隔离晚到消息，取消仅拆分析节点，绝不停止 LiveKit 的原始轨道。 */
  async attach(track: RemoteAudioTrack, signal?: AbortSignal): Promise<void> {
    if (this.disposed) throw new Error('音频桥接已经释放');
    this.releaseAttachment();
    const attachment: Attachment = { controller: new AbortController(), cleanups: [] };
    this.attachment = attachment;
    /** 外部连接取消既中断模块等待，也释放已建立的静音支路。 */
    const abort = () => {
      if (this.attachment === attachment) this.releaseAttachment();
    };
    if (signal?.aborted) {
      this.releaseAttachment();
      throw new DOMException('音频分析已取消', 'AbortError');
    }
    signal?.addEventListener('abort', abort, { once: true });
    /** 保存对应监听引用，轨道替换时解除与旧会话 AbortSignal 的关系。 */
    attachment.cleanups.push(() => signal?.removeEventListener('abort', abort));
    try {
      await waitForModule(loadWorklet(this.context), attachment.controller.signal);
      if (this.attachment !== attachment || attachment.controller.signal.aborted || this.disposed) {
        throw new DOMException('音频分析已取消', 'AbortError');
      }
      if (this.context.state === 'closed') throw new Error('会话音频上下文已经关闭');
      const mediaTrack = track.mediaStreamTrack;
      if (mediaTrack.kind !== 'audio' || mediaTrack.readyState === 'ended') {
        throw new Error('助手音轨不可用');
      }
      attachment.source = this.context.createMediaStreamSource(new MediaStream([mediaTrack]));
      attachment.worklet = new AudioWorkletNode(this.context, 'avatar-pcm', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCount: 1,
        channelCountMode: 'explicit',
      });
      attachment.gain = this.context.createGain();
      attachment.gain.gain.value = 0;
      /** 播放未就绪与轨道静音时丢弃样本，恢复时也不能消费 MessagePort 内积压的旧窗口。 */
      attachment.worklet.port.onmessage = (event: MessageEvent) => {
        const { samples, sampleRate, at, sequence } = event.data ?? {};
        if (Number.isInteger(sequence)) {
          attachment.worklet?.port.postMessage({ type: 'ack', sequence });
        }
        if (
          this.attachment !== attachment ||
          !this.playbackAvailable ||
          this.context.state !== 'running' ||
          track.isMuted ||
          mediaTrack.readyState === 'ended'
        ) {
          return;
        }
        const outputLatency = this.context.outputLatency;
        const baseLatency = this.context.baseLatency;
        const validAge =
          0.15 +
          (Number.isFinite(outputLatency) ? Math.max(0, outputLatency) : 0) +
          (Number.isFinite(baseLatency) ? Math.max(0, baseLatency) : 0);
        if (
          !(samples instanceof Float32Array) ||
          samples.length === 0 ||
          !Number.isFinite(sampleRate) ||
          sampleRate !== this.context.sampleRate ||
          !Number.isFinite(at) ||
          at < this.availableSince ||
          this.context.currentTime - at > validAge
        ) {
          return;
        }
        try {
          this.onSamples(samples, sampleRate, at);
        } catch (error) {
          this.releaseAttachment();
          this.reportError(error);
        }
      };
      /** 处理器运行期崩溃不能伪装成正常口型，释放支路并把故障交给页面恢复入口。 */
      attachment.worklet.onprocessorerror = () => {
        if (this.attachment !== attachment) return;
        this.releaseAttachment();
        this.reportError(new Error('音频分析处理器异常'));
      };
      /** SDK 或原始媒体轨道终止都要立即清除保持中的嘴型。 */
      const ended = () => {
        if (this.attachment === attachment) this.releaseAttachment();
      };
      /** 远端静音可以先于新的零采样抵达，先清嘴再等待后续音频窗口。 */
      const muted = () => {
        if (this.attachment === attachment) this.resetSamples();
      };
      /** SDK 重新创建媒体轨道时重新绑定公共媒体轨属性，避免一直分析已经废弃的流。 */
      const restarted = () => {
        if (this.attachment !== attachment) return;
        /** 取消是正常生命周期，真正的重新绑定失败则通过故障回调向上暴露。 */
        void this.attach(track, signal).catch((error: unknown) => {
          if (!(error instanceof DOMException && error.name === 'AbortError'))
            this.reportError(error);
        });
      };
      mediaTrack.addEventListener('ended', ended);
      track.on('ended', ended);
      track.on('muted', muted);
      track.on('unmuted', muted);
      track.on('restarted', restarted);
      /** 所有监听由同一附件回收，重复连接不会给 SDK 轨道遗留引用。 */
      attachment.cleanups.push(() => {
        mediaTrack.removeEventListener('ended', ended);
        track.off('ended', ended);
        track.off('muted', muted);
        track.off('unmuted', muted);
        track.off('restarted', restarted);
      });
      attachment.source.connect(attachment.worklet);
      attachment.worklet.connect(attachment.gain);
      attachment.gain.connect(this.context.destination);
      this.resetSamples();
    } catch (error) {
      if (this.attachment === attachment) this.releaseAttachment();
      throw error;
    }
  }

  /** 由 LiveKit 的实际播放许可控制口型，恢复声音时从当前时钟开始而不追赶旧消息。 */
  setPlaybackAvailable(available: boolean): void {
    if (this.disposed || this.playbackAvailable === available) return;
    this.playbackAvailable = available;
    this.resetSamples();
  }

  /** 动画卸载只释放借用的分析资源，上下文关闭继续由会话所有者负责。 */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.context.removeEventListener('statechange', this.handleContextState);
    this.releaseAttachment();
  }

  /** 浏览器暂停时立即收嘴，恢复后丢弃暂停之前已经投递到主线程的 PCM。 */
  private handleContextState = (): void => {
    if (this.disposed) return;
    this.resetSamples();
  };

  /** 同时重置前端状态与处理器的半窗口，旧嘴型和下一批音频保持同一恢复边界。 */
  private resetSamples(): void {
    this.availableSince = this.context.currentTime;
    this.attachment?.worklet?.port.postMessage({ type: 'reset' });
    this.onSilent();
  }

  /** 先使附件失效再拆图，异步消息和取消回调即使重入也只能看到空附件。 */
  private releaseAttachment(): void {
    const attachment = this.attachment;
    this.attachment = null;
    if (attachment) {
      attachment.controller.abort();
      for (const cleanup of attachment.cleanups) cleanup();
      if (attachment.worklet) {
        attachment.worklet.port.onmessage = null;
        attachment.worklet.onprocessorerror = null;
      }
      attachment.source?.disconnect();
      attachment.worklet?.disconnect();
      attachment.gain?.disconnect();
      attachment.worklet?.port.close();
    }
    this.onSilent();
  }

  /** 加载错误由 attach 拒绝传播，已绑定后的错误使用回调，缺省时仍留下可见诊断。 */
  private reportError(error: unknown): void {
    const failure = error instanceof Error ? error : new Error('音频分析失败', { cause: error });
    if (this.onError) this.onError(failure);
    else console.error(failure);
  }
}
