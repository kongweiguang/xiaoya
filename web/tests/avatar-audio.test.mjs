import assert from 'node:assert/strict';
import { EventEmitter, getEventListeners } from 'node:events';
import { readFile } from 'node:fs/promises';
import { after, test } from 'node:test';
import vm from 'node:vm';
import { AvatarAudioBridge, createSessionAudio } from '../lib/avatar/audio-bridge.ts';
import {
  AudioLevelLipSync,
  LipSyncTimeline,
  getAudibleAudioTime,
  rmsAmplitude,
} from '../lib/avatar/lip-sync.ts';

/** 延迟模块加载用可控 Promise 表达，验证取消竞态无需真实网络或定时睡眠。 */
function deferred() {
  let resolve;
  let reject;
  /** 保留完成句柄，测试负责明确推进每个异步边界。 */
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

class FakeNode {
  connections = [];
  disconnects = 0;

  /** 保存连接拓扑，测试能发现误接到扬声器的非静音支路。 */
  connect(target) {
    this.connections.push(target);
    return target;
  }

  /** 记录真正的节点释放，而不是只检查 JavaScript 引用是否被删除。 */
  disconnect() {
    this.connections = [];
    this.disconnects += 1;
  }
}

class FakePort {
  onmessage = null;
  messages = [];
  closed = false;

  /** 模拟控制消息，检查恢复播放是否重置处理器的半个旧窗口。 */
  postMessage(message) {
    this.messages.push(message);
  }

  /** MessagePort 关闭是节点之外的另一项独立资源，测试单独验收。 */
  close() {
    this.closed = true;
  }

  /** 在主线程侧注入工作线程输出，保持测试不依赖浏览器媒体权限。 */
  emit(data) {
    this.onmessage?.({ data });
  }
}

class FakeWorklet extends FakeNode {
  port = new FakePort();
  onprocessorerror = null;

  /** 模拟浏览器创建节点，所有节点保留到上下文中供释放断言检查。 */
  constructor(context, name, options) {
    super();
    this.name = name;
    this.options = options;
    context.worklets.push(this);
  }
}

class FakeContext extends EventTarget {
  state = 'running';
  currentTime = 1;
  sampleRate = 48_000;
  destination = new FakeNode();
  worklets = [];
  sources = [];
  gains = [];
  moduleCalls = [];
  modulePromise = Promise.resolve();
  closeCalls = 0;
  audioWorklet = {
    /** 模块加载结果由测试控制，用于覆盖真实加载期间挂断的情况。 */
    addModule: (url) => {
      this.moduleCalls.push(url);
      return this.modulePromise;
    },
  };

  /** 保留流引用，确认分析原始助手轨道而没有 clone 或 stop 原轨。 */
  createMediaStreamSource(stream) {
    const source = new FakeNode();
    source.stream = stream;
    this.sources.push(source);
    return source;
  }

  /** 用普通节点加上增益状态模拟零增益出口，不引入可听声音。 */
  createGain() {
    const gain = new FakeNode();
    gain.gain = { value: 1 };
    this.gains.push(gain);
    return gain;
  }

  /** 测试关闭所有权，bridge 不应调用这个方法。 */
  close() {
    this.closeCalls += 1;
    this.state = 'closed';
    return Promise.resolve();
  }
}

class FakeMediaTrack extends EventTarget {
  kind = 'audio';
  readyState = 'live';
  stopCalls = 0;

  /** 如果 bridge 错停 SDK 轨道，测试会记录并失败。 */
  stop() {
    this.stopCalls += 1;
    this.readyState = 'ended';
  }
}

class FakeTrack extends EventEmitter {
  mediaStreamTrack = new FakeMediaTrack();
  isMuted = false;
}

class FakeStream {
  /** 只保存媒体轨，不需要模拟浏览器的额外轨道所有权。 */
  constructor(tracks) {
    this.tracks = tracks;
  }
}

const originalGlobals = new Map();
for (const [name, value] of [
  ['AudioContext', FakeContext],
  ['AudioWorkletNode', FakeWorklet],
  ['MediaStream', FakeStream],
]) {
  originalGlobals.set(name, globalThis[name]);
  globalThis[name] = value;
}
/** 测试退出恢复宿主全局，避免影响同一 Node 进程中的其他浏览器模拟。 */
after(() => {
  for (const [name, value] of originalGlobals) {
    if (value === undefined) delete globalThis[name];
    else globalThis[name] = value;
  }
});

/** 使用真实波形和时间戳，避免测试仅镜像实现分支。 */
function packet(at, value = 0.2) {
  return { samples: new Float32Array(960).fill(value), sampleRate: 48_000, at };
}

/** 收集外部可见结果，测试既检查用户行为也检查资源所有权。 */
function makeBridge(context = new FakeContext()) {
  const samples = [];
  const errors = [];
  const observation = { silent: 0 };
  const bridge = new AvatarAudioBridge({
    context,
    /** 样本记录保留参数，验证音频窗口与同一音频时钟没有被重写。 */
    onSamples: (...arguments_) => samples.push(arguments_),
    /** 收嘴回调代表运行时持有口型被主动清除。 */
    onSilent: () => {
      observation.silent += 1;
    },
    /** 运行期失败必须对调用方可见，不能只在内部吞掉。 */
    onError: (error) => errors.push(error),
  });
  return { bridge, context, samples, errors, observation };
}

/** 输出设备延迟必须校正，过期的输出时间戳回退到公开延迟值。 */
test('输出时钟优先使用设备时间戳，缺失或过期时使用输出延迟', () => {
  const context = {
    currentTime: 2,
    baseLatency: 0.01,
    outputLatency: 0.08,
    /** 构造当前设备时间戳，使画面位于实际已播放的音频时刻。 */
    getOutputTimestamp: () => ({ contextTime: 1.9, performanceTime: 1_000 }),
  };
  assert.equal(getAudibleAudioTime(context, 1_020), 1.92);
  assert.equal(getAudibleAudioTime(context, 2_001), 1.91);
  assert.equal(getAudibleAudioTime({ currentTime: 0.03, outputLatency: 0.1 }), 0);
  assert.equal(getAudibleAudioTime({ currentTime: 2, outputLatency: NaN }), 2);
});

/** 在未来音频到来前保持闭嘴，停止采样后自动清除残留的张嘴帧。 */
test('口型仅选择已经播放的帧，并在停止采样后及时闭嘴', () => {
  const timeline = new LipSyncTimeline();
  timeline.push({ at: 1, open: 0.8, form: 0.3 });
  timeline.push({ at: 1.04, open: 0.2, form: -0.5 });
  assert.equal(timeline.select(0.99).open, 0);
  assert.equal(timeline.select(1.02).open, 0.8);
  assert.equal(timeline.select(1.04).form, -0.5);
  assert.equal(timeline.select(1.17).open, 0);
  assert.equal(timeline.size, 0);
});

/** 延迟消息不得重新打开旧嘴型，有界队列也不能随后台停留时长增长。 */
test('积压口型保持有界，迟到帧被拒绝，新会话重置时序栅栏', () => {
  const timeline = new LipSyncTimeline({ maxFrames: 3 });
  for (let index = 0; index < 100; index += 1) {
    timeline.push({ at: 10 + index * 0.02, open: 0.4, form: 0 });
  }
  assert.equal(timeline.size, 3);
  assert.equal(timeline.push({ at: 10, open: 1, form: 0 }), false);
  assert.equal(timeline.push({ at: NaN, open: 1, form: 0 }), false);
  assert.equal(timeline.select(100).open, 0);
  timeline.clear();
  assert.equal(timeline.push({ at: 0.01, open: 2, form: -2 }), true);
  assert.deepEqual(timeline.select(0.01), { at: 0.01, open: 1, form: -1 });
  assert.equal(timeline.push({ at: 0.01, open: 0.5, form: 0 }), true);
  assert.equal(timeline.select(0.01).open, 0.5);
});

/** 静音门限抑制噪声，并在停音 100 ms 内完成收嘴，不能等到下一句才恢复。 */
test('音量降级快速开口、平滑收口，静音窗口与无效 PCM 不留下残留', () => {
  const analyzer = new AudioLevelLipSync();
  const speech = new Float32Array(960).fill(0.18);
  const noise = new Float32Array(960).fill(0.005);
  assert.equal(analyzer.sample(noise, 48_000, 0.01).open, 0);
  const first = analyzer.sample(speech, 48_000, 0.03).open;
  const second = analyzer.sample(speech, 48_000, 0.05).open;
  assert.ok(first > 0.4 && second > first);
  assert.ok(analyzer.sample(noise, 48_000, 0.07).open < second);
  const late = analyzer.sample(speech, 48_000, 0.01);
  assert.equal(late.at, 0.07);
  for (const at of [0.09, 0.11, 0.13, 0.15, 0.17]) analyzer.sample(noise, 48_000, at);
  assert.equal(analyzer.sample(noise, 48_000, 0.19).open, 0);
  analyzer.reset();
  assert.equal(analyzer.sample(new Float32Array([NaN, Infinity]), 48_000, 0.01).open, 0);
  assert.equal(rmsAmplitude(new Float32Array([0.3, -0.3])), 0.30000001192092896);
});

/** 对同一助手轨的分析必须保持无声、共享时钟并释放所有借用节点。 */
test('分析分支只借用助手音轨，重复释放不停止轨道或关闭会话时钟', async () => {
  const { bridge, context, samples } = makeBridge();
  const track = new FakeTrack();
  await bridge.attach(track);
  const worklet = context.worklets[0];
  assert.equal(context.sources[0].stream.tracks[0], track.mediaStreamTrack);
  assert.equal(context.gains[0].gain.value, 0);
  assert.deepEqual(context.sources[0].connections, [worklet]);
  assert.deepEqual(worklet.connections, [context.gains[0]]);
  assert.deepEqual(context.gains[0].connections, [context.destination]);
  worklet.port.emit(packet(1.01));
  assert.equal(samples.length, 0);
  bridge.setPlaybackAvailable(true);
  worklet.port.emit(packet(1.02));
  assert.equal(samples.length, 1);
  assert.equal(samples[0][1], context.sampleRate);
  assert.equal(samples[0][2], 1.02);
  bridge.dispose();
  bridge.dispose();
  assert.equal(track.mediaStreamTrack.stopCalls, 0);
  assert.equal(context.closeCalls, 0);
  assert.equal(track.eventNames().length, 0);
  assert.equal(worklet.port.closed, true);
  assert.equal(context.sources[0].disconnects, 1);
  assert.equal(worklet.disconnects, 1);
  assert.equal(context.gains[0].disconnects, 1);
});

/** 旧 MessagePort 回调已经进入队列时，替换音轨也必须能阻止旧样本进入口型。 */
test('轨道替换隔离旧消息，播放恢复丢弃原先积压的 PCM', async () => {
  const { bridge, context, samples } = makeBridge();
  bridge.setPlaybackAvailable(true);
  const old = new FakeTrack();
  await bridge.attach(old);
  const staleMessage = context.worklets[0].port.onmessage;
  await bridge.attach(new FakeTrack());
  staleMessage({ data: packet(1.01) });
  assert.equal(samples.length, 0);
  assert.equal(old.eventNames().length, 0);
  assert.equal(context.moduleCalls.length, 1);
  context.worklets[1].port.emit(packet(1.02));
  assert.equal(samples.length, 1);
  bridge.setPlaybackAvailable(false);
  context.currentTime = 2;
  bridge.setPlaybackAvailable(true);
  context.worklets[1].port.emit(packet(1.5));
  context.worklets[1].port.emit(packet(2.01));
  assert.equal(samples.length, 2);
  context.currentTime = 3;
  context.worklets[1].port.emit({ ...packet(2.05), sequence: 10 });
  assert.equal(samples.length, 2);
  assert.deepEqual(context.worklets[1].port.messages.at(-1), { type: 'ack', sequence: 10 });
  bridge.dispose();
});

/** 浏览器暂停和远端静音都不依赖下一帧动画，立即通知上层清空保持状态。 */
test('静音、上下文暂停和轨道结束会及时清嘴，恢复后等待新音频', async () => {
  const { bridge, context, samples, observation } = makeBridge();
  const track = new FakeTrack();
  bridge.setPlaybackAvailable(true);
  await bridge.attach(track);
  const port = context.worklets[0].port;
  port.emit(packet(1.02));
  const beforeMuted = observation.silent;
  track.isMuted = true;
  track.emit('muted');
  port.emit(packet(1.03));
  assert.ok(observation.silent > beforeMuted);
  assert.equal(samples.length, 1);
  track.isMuted = false;
  track.emit('unmuted');
  context.state = 'suspended';
  context.dispatchEvent(new Event('statechange'));
  port.emit(packet(1.04));
  assert.equal(samples.length, 1);
  context.currentTime = 2;
  context.state = 'running';
  context.dispatchEvent(new Event('statechange'));
  port.emit(packet(1.99));
  port.emit(packet(2.01));
  assert.equal(samples.length, 2);
  track.mediaStreamTrack.dispatchEvent(new Event('ended'));
  assert.equal(port.closed, true);
  bridge.dispose();
});

/** 模块尚未到达时取消，后续加载成功也不能再创建节点或重新打开旧会话。 */
test('取消与晚到的 addModule 结果不会复活已经释放的附件', async () => {
  const context = new FakeContext();
  const loading = deferred();
  context.modulePromise = loading.promise;
  const { bridge } = makeBridge(context);
  const controller = new AbortController();
  const attaching = bridge.attach(new FakeTrack(), controller.signal);
  controller.abort();
  await assert.rejects(attaching, { name: 'AbortError' });
  loading.resolve();
  await Promise.resolve();
  assert.equal(context.sources.length, 0);
  assert.equal(context.worklets.length, 0);
  bridge.dispose();
});

/** 已经成功绑定后的取消和多次会话切换也必须回收节点、媒体及上下文监听。 */
test('连续 20 次连接与挂断不累积监听，不停止 SDK 音轨', async () => {
  const context = new FakeContext();
  for (let index = 0; index < 20; index += 1) {
    const { bridge } = makeBridge(context);
    const controller = new AbortController();
    const track = new FakeTrack();
    await bridge.attach(track, controller.signal);
    assert.equal(getEventListeners(controller.signal, 'abort').length, 1);
    controller.abort();
    assert.equal(track.eventNames().length, 0);
    assert.equal(getEventListeners(track.mediaStreamTrack, 'ended').length, 0);
    assert.equal(track.mediaStreamTrack.stopCalls, 0);
    bridge.dispose();
    assert.equal(getEventListeners(context, 'statechange').length, 0);
  }
  assert.equal(context.moduleCalls.length, 1);
  for (const worklet of context.worklets) {
    assert.equal(worklet.port.closed, true);
    assert.equal(worklet.disconnects, 1);
  }
  assert.ok(context.sources.every((node) => node.disconnects === 1));
  assert.ok(context.gains.every((node) => node.disconnects === 1));
  assert.equal(context.closeCalls, 0);
});

/** 模块加载失败允许再次请求；处理器崩溃则通过明确的错误回调提供恢复入口。 */
test('加载错误向上传播并允许重试，运行期错误释放节点后报告', async () => {
  const context = new FakeContext();
  context.modulePromise = Promise.reject(new Error('模块下载失败'));
  const { bridge, errors } = makeBridge(context);
  await assert.rejects(bridge.attach(new FakeTrack()), /模块下载失败/);
  context.modulePromise = Promise.resolve();
  await bridge.attach(new FakeTrack());
  assert.equal(context.moduleCalls.length, 2);
  context.worklets[0].onprocessorerror();
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /处理器异常/);
  assert.equal(context.worklets[0].port.closed, true);
  bridge.dispose();
});

/** 会话关闭只发生一次，模型桥接释放不会越过自身所有权边界。 */
test('会话时钟由所有者关闭，重复请求复用同一关闭 Promise', async () => {
  const session = createSessionAudio();
  const first = session.close();
  assert.equal(first, session.close());
  await first;
  assert.equal(session.context.closeCalls, 1);
});

/** 在独立音频线程环境执行真实处理器，校验 20 ms 分块、时钟和静音输出。 */
test('PCM worklet 在 44.1 kHz 和 48 kHz 下都输出 20 ms 单声道中心时间', async () => {
  const source = await readFile(
    new URL('../public/avatar/pcm-worklet.js', import.meta.url),
    'utf8'
  );
  for (const rate of [44_100, 48_000]) {
    let Processor;
    const packets = [];
    const sandbox = vm.createContext({
      sampleRate: rate,
      currentFrame: 0,
      Float32Array,
      AudioWorkletProcessor: class {
        /** 工作线程只需要消息出口，消息保存在外部以检查真实输出。 */
        constructor() {
          this.port = {
            onmessage: null,
            /** 不序列化 PCM，保留原精度验证立体声平均和窗口长度。 */
            postMessage: (message) => packets.push(message),
          };
        }
      },
      /** 提取真实注册类，同时验证处理器标识与桥接构造参数一致。 */
      registerProcessor: (name, implementation) => {
        assert.equal(name, 'avatar-pcm');
        Processor = implementation;
      },
    });
    vm.runInContext(source, sandbox);
    const processor = new Processor();
    for (let index = 0; index < 80; index += 1) {
      const output = new Float32Array(128).fill(1);
      const input = [new Float32Array(128).fill(0.2), new Float32Array(128).fill(0.4)];
      assert.equal(processor.process([input], [[output]]), true);
      assert.ok(output.every((sample) => sample === 0));
      sandbox.currentFrame += 128;
    }
    assert.equal(packets.length, 4);
    assert.equal(packets[0].samples.length, Math.round(rate * 0.02));
    assert.equal(packets[0].at, 0.01);
    assert.equal(packets[1].at, 0.03);
    assert.ok(Math.abs(packets[0].samples[0] - 0.3) < 0.000_001);
    const resumedAt = sandbox.currentFrame / rate;
    processor.port.onmessage({ data: { type: 'ack', sequence: packets[0].sequence } });
    for (let index = 0; index < 12; index += 1) {
      processor.process([[new Float32Array(128)]], [[new Float32Array(128)]]);
      sandbox.currentFrame += 128;
    }
    assert.equal(packets.length, 5);
    assert.ok(packets.at(-1).at >= resumedAt - 0.01);
    processor.port.onmessage({ data: { type: 'reset' } });
    packets.length = 0;
    const resetFrame = sandbox.currentFrame;
    for (let index = 0; index < 8; index += 1) {
      processor.process([[new Float32Array(128)]], [[new Float32Array(128)]]);
      sandbox.currentFrame += 128;
    }
    assert.ok(Math.abs(packets[0].at - (resetFrame / rate + 0.01)) < 1e-12);
    assert.ok(packets[0].samples.every((sample) => sample === 0));
  }
});
