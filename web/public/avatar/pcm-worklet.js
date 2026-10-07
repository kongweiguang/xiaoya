/* global AudioWorkletProcessor, currentFrame, sampleRate, registerProcessor */

class AvatarPcmProcessor extends AudioWorkletProcessor {
  /** 20 ms 窗口与渲染块大小独立，避免 44.1 kHz 和 48 kHz 下产生不同同步延迟。 */
  constructor() {
    super();
    this.windowSize = Math.round(sampleRate * 0.02);
    this.samples = new Float32Array(this.windowSize);
    this.offset = 0;
    this.startFrame = 0;
    this.sequence = 0;
    this.pending = new Set();
    /** 播放恢复和轨道切换后抛弃半个旧窗口，不能把之前的声音带到新口型。 */
    this.port.onmessage = (event) => {
      if (event.data?.type === 'reset') {
        this.offset = 0;
        this.pending.clear();
      } else if (event.data?.type === 'ack') {
        this.pending.delete(event.data.sequence);
      }
    };
  }

  /** 输出保持零值，仅把单声道采样送往主线程；真正声音由 LiveKit 的播放图输出。 */
  process(inputs, outputs) {
    for (const output of outputs) {
      for (const channel of output) channel.fill(0);
    }
    const channels = inputs[0];
    const blockSize = channels?.[0]?.length ?? outputs[0]?.[0]?.length ?? 128;
    for (let index = 0; index < blockSize; index += 1) {
      if (this.offset === 0) this.startFrame = currentFrame + index;
      let sample = 0;
      if (channels?.length) {
        for (const channel of channels) sample += channel[index] ?? 0;
        sample /= channels.length;
      }
      this.samples[this.offset] = Number.isFinite(sample) ? sample : 0;
      this.offset += 1;
      if (this.offset === this.windowSize) {
        if (this.pending.size < 4) {
          const samples = this.samples;
          const sequence = this.sequence;
          this.sequence += 1;
          this.pending.add(sequence);
          this.port.postMessage(
            {
              samples,
              sampleRate,
              sequence,
              at: (this.startFrame + this.windowSize / 2) / sampleRate,
            },
            [samples.buffer]
          );
          this.samples = new Float32Array(this.windowSize);
        }
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor('avatar-pcm', AvatarPcmProcessor);
