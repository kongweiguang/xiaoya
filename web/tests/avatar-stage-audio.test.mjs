import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, test } from 'node:test';
import { AudioEnvelopeTimeline } from '../lib/avatar/audio-bridge.ts';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { RemoteTrack, conversationPorts, delivery } from './helpers/conversation-ports.mjs';
import {
  React,
  cleanup,
  deferred,
  fireEvent,
  media,
  production,
  render,
  settle,
} from './helpers/dom.mjs';

/** 清理真实 React 副作用并恢复媒体偏好，避免前一场景的降级设置污染下一场景。 */
afterEach(() => {
  cleanup();
  media.matches = false;
});

/** 模型、WebAudio 与分析器作为外部端口；整个 AvatarStage 的真实 React 生命周期不提取或复制。 */
async function stage(options = {}) {
  const environment = conversationPorts();
  const instances = [];
  const bridges = [];
  const analyzers = [];
  let loads = 0;
  const runtime = {
    /** GPU 建立可被延迟或失败，组件必须独立拥有并正确处理迟到结果。 */
    async load(_canvas, callbacks, signal) {
      loads++;
      if (options.failFirst && loads === 1) throw new Error('GPU unavailable');
      if (options.loading) await options.loading.promise;
      const instance = {
        model: {},
        motionSyncBuffer: new ArrayBuffer(8),
        callbacks,
        signal,
        disposed: 0,
        /** 参数更新不销毁 GPU 或触发新资源加载。 */
        setBehavior() {},
        /** 每个 runtime 单独计数，取消后的迟到模型也必须归原舞台回收。 */
        dispose() {
          this.disposed++;
        },
      };
      instances.push(instance);
      return instance;
    },
  };
  class Bridge {
    /** 借用会话时钟，验证舞台重试不会创建或关闭额外的播放出口。 */
    constructor(config) {
      this.config = config;
      this.disposed = 0;
      bridges.push(this);
    }
    /** 绑定只记录分析源，实际音频桥的零增益与不 stop 原轨由 avatar-audio 覆盖。 */
    async attach(track) {
      this.track = track;
    }
    /** 可听许可仍传给分析端口，不凭 speaking 判断有声音。 */
    setPlaybackAvailable(value) {
      this.available = value;
    }
    /** 替换和卸载都必须释放借用的分析端口。 */
    dispose() {
      this.disposed++;
    }
  }
  const { AvatarStage } = await production('components/app/avatar-stage.tsx', {
    ...environment.ports,
    '@/lib/avatar/live2d-runtime': { Live2DRuntime: runtime },
    '@/lib/avatar/audio-bridge': { AvatarAudioBridge: Bridge, AudioEnvelopeTimeline },
    '@/lib/avatar/sdk-loader': { loadSdkScript: async () => undefined },
    '@/lib/avatar/motion-sync': {
      /** 官方分析库属于外部 native 端口，测试只验证它的所有权和失效边界。 */
      createMotionSyncAnalyzer: async () => {
        const analyzer = {
          mode: 'motionsync',
          disposed: 0,
          /** 输入端口可产生真实非零帧，空队列不能检出停止后迟到 PCM 重新张嘴的竞态。 */
          sample(_samples, _rate, at) {
            return options.nonzeroLip ? [{ at, open: 0.87, form: 0.3 }] : [];
          },
          /** 本场景不模拟 native 缓冲，仅保留分析器重置端口供真实舞台调用。 */
          reset() {},
          /** 记录外部资源释放次数，重复回收不能被无操作 mock 掩盖。 */
          dispose() {
            this.disposed++;
          },
        };
        analyzers.push(analyzer);
        return analyzer;
      },
    },
  });
  const room = new environment.ports['livekit-client'].Room({});
  await room.start();
  const context = new AudioContext();
  await context.resume();
  const input = {
    room,
    audio: { context },
    peer: { state: 'speaking', agent: environment.agent, track: new RemoteTrack() },
    connected: true,
    reconnecting: false,
    status: '正在和你说话',
    delivery: { current: new DeliveryGate() },
    messages: [],
  };
  const view = render(React.createElement(AvatarStage, input));
  await settle();
  return {
    ...environment,
    ...view,
    input,
    instances,
    bridges,
    analyzers,
    loads: () => loads,
    /** 同一个组件位置改变 props，确保测试能发现换 Room 导致的意外 GPU 重载。 */
    update(props) {
      view.rerender(React.createElement(AvatarStage, props));
    },
  };
}

/** Room 资源每次尝试独立，角色 GPU 则是持久页面资源，换房只能重绑静音分析支路。 */
test('更换房间仅重绑音频，不重新加载模型与纹理', async () => {
  const view = await stage();
  assert.equal(view.loads(), 1);
  assert.equal(view.bridges.length, 1);
  const nextRoom = new view.ports['livekit-client'].Room({});
  await nextRoom.start();
  const nextContext = new AudioContext();
  await nextContext.resume();
  const nextTrack = new RemoteTrack();
  await settle(() =>
    view.update({
      ...view.input,
      room: nextRoom,
      audio: { context: nextContext },
      peer: { ...view.input.peer, track: nextTrack },
    })
  );
  assert.equal(view.loads(), 1);
  assert.equal(view.instances[0].disposed, 0);
  assert.equal(view.bridges[0].disposed, 1);
  assert.equal(view.analyzers[0].disposed, 1);
  assert.equal(view.bridges[1].track, nextTrack);
  assert.equal(view.input.audio.context.closes, 0);
  assert.equal(view.input.peer.track.stops, 0);
  view.unmount();
  assert.equal(view.instances[0].disposed, 1);
  assert.equal(view.bridges[1].disposed, 1);
  assert.equal(nextContext.closes, 0);
  assert.equal(nextTrack.stops, 0);
});

/** GPU 失败只影响画面，用户仍能看到静态原画并显式重试，不能制造一个新会话。 */
test('模型失败显示静态后备，同一舞台重试恢复动画', async () => {
  const view = await stage({ failFirst: true });
  assert.equal(view.container.querySelector('.avatar-media').dataset.renderer, 'static');
  assert.match(view.getByRole('button', { name: '重试动画' }).textContent, /重试/);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试动画' })));
  assert.equal(view.loads(), 2);
  assert.equal(view.container.querySelector('.avatar-media').dataset.renderer, 'live2d');
  assert.equal(view.contexts.length, 1);
  assert.equal(view.rooms.length, 1);
});

/** 系统偏好改变只关闭画面；回复错过的动作必须截断，时钟和真正音轨继续由会话持有。 */
test('减少动态效果释放 GPU 和分析但保留当前音频时钟', async () => {
  const view = await stage();
  await settle(() => {
    media.matches = true;
    media.dispatchEvent(new Event('change'));
  });
  assert.equal(view.container.querySelector('canvas'), null);
  assert.equal(view.instances[0].disposed, 1);
  assert.equal(view.bridges[0].disposed, 1);
  assert.equal(view.input.audio.context.closes, 0);
  await settle(() => {
    media.matches = false;
    media.dispatchEvent(new Event('change'));
  });
  assert.equal(view.loads(), 2);
  assert.equal(view.container.querySelector('.avatar-media').dataset.renderer, 'live2d');
});

/** 外部模型加载不一定遵守 AbortSignal，卸载后的成功也只能释放原模型，不再绑定分析。 */
test('卸载后迟到模型成功被释放，不复活分析与画面', async () => {
  const loading = deferred();
  const view = await stage({ loading });
  view.unmount();
  await settle(() => loading.resolve());
  assert.equal(view.instances.length, 1);
  assert.equal(view.instances[0].disposed, 1);
  assert.equal(view.bridges.length, 0);
  assert.equal(view.input.audio.context.closes, 0);
});

/** 组件实际无音频许可时仍显示倾听；独立视觉门不能误放行说话手势。 */
test('倾听微表情不要求音轨或可听上下文，说话仍保留播放门', async () => {
  const view = await stage();
  view.input.room.canPlaybackAudio = false;
  view.input.audio.context.state = 'suspended';
  await settle(() =>
    view.update({
      ...view.input,
      peer: { ...view.input.peer, state: 'listening', track: undefined },
    })
  );
  const listening = delivery(1, {
    state: 'active',
    phase: 'listening',
    turn_id: 'user-turn',
    style: 'concerned',
  });
  view.input.delivery.current.snapshot(listening);
  const read = view.instances[0].callbacks.readDelivery;
  assert.equal(read().phase, 'listening');
  assert.equal(read().style, 'concerned');
  const speaking = delivery(2, {
    state: 'active',
    phase: 'speaking',
    turn_id: 'user-turn',
    reply_id: 'reply',
    segment_id: 'segment',
    gesture: 'wave',
  });
  view.input.delivery.current.receive(speaking);
  view.input.delivery.current.offer(
    [
      {
        id: 'segment',
        message: '你好',
        from: { identity: 'agent', isLocal: false },
        attributes: { 'xiaoya.delivery': JSON.stringify(speaking), 'lk.segment_id': 'segment' },
      },
    ],
    'agent'
  );
  assert.equal(read(), null);
});

/** 使用合法 v3 字幕绑定当前回复；测试不能绕过身份门直接给舞台注入表现对象。 */
async function authorizeReply(view, revision, reply, overrides = {}) {
  const state = delivery(revision, {
    state: 'active',
    phase: 'speaking',
    turn_id: `turn-${reply}`,
    reply_id: reply,
    segment_id: `segment-${reply}`,
    gesture: 'nod',
    ...overrides,
  });
  if (revision === 1) view.input.delivery.current.snapshot(delivery(0));
  await settle(() => view.input.delivery.current.receive(state));
  const messages = [
    {
      id: state.segment_id,
      message: '你好',
      from: { identity: 'agent', isLocal: false },
      attributes: {
        'xiaoya.delivery': JSON.stringify(state),
        'lk.segment_id': state.segment_id,
      },
    },
  ];
  view.input = { ...view.input, messages };
  await settle(() => view.update(view.input));
  return state;
}

/** 设备输出延迟会让合法帧暂时位于未来；等待首次可听不能丢队列导致永远没有首个开口。 */
test('已匹配新回复的未来 PCM 保留到实际输出时间，不额外等待或误取消首声', async () => {
  const view = await stage({ nonzeroLip: true });
  const callbacks = view.instances[0].callbacks;
  await authorizeReply(view, 1, 'future-audio');
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.06);
  assert.equal(callbacks.readLip(), null);
  view.input.audio.context.currentTime = 0.04;
  assert.equal(callbacks.readLip(), null);
  view.input.audio.context.currentTime = 0.06;
  assert.equal(callbacks.readLip().open, 0.87);
  assert.equal(callbacks.readDelivery().gesture, 'nod');
});

/** 工具阶段的SDK暂停不同于设备失效；可靠新段可先到，但仅自己的新PCM才能重新张嘴。 */
test('真实舞台工具暂停清旧嘴，新段先于SDK恢复仍可在自身PCM开口', async () => {
  const view = await stage({ nonzeroLip: true });
  const callbacks = view.instances[0].callbacks;
  const gate = view.input.delivery.current;
  const first = await authorizeReply(view, 1, 'tool-reply');
  view.input.audio.context.currentTime = 0.1;
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.1);
  assert.equal(callbacks.readLip()?.open, 0.87);
  // 权威新句段先于SDK thinking回调；旧displayed已撤，不能把待播新段误封为旧段。
  gate.receive({
    ...first,
    revision: 2,
    phase: 'thinking',
    reply_id: '',
    segment_id: '',
    gesture: 'none',
    gesture_id: '',
    gesture_source: 'none',
  });
  await authorizeReply(view, 3, 'tool-reply', { segment_id: 'tool-next' });
  view.input = { ...view.input, peer: { ...view.input.peer, state: 'thinking' } };
  await settle(() => view.update(view.input));
  assert.equal(callbacks.readLip(), null);
  assert.equal(callbacks.readDelivery(), null);
  assert.equal(callbacks.readDelivery(), null, '连续rAF等待不能取消新候选');
  view.input = { ...view.input, peer: { ...view.input.peer, state: 'speaking' } };
  await settle(() => view.update(view.input));
  assert.equal(callbacks.readLip(), null, '旧PCM尾窗不能借给新段');
  view.input.audio.context.currentTime = 0.2;
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.2);
  assert.equal(callbacks.readLip()?.open, 0.87);
  assert.equal(
    callbacks.readDelivery()?.segmentKey,
    JSON.stringify(['job-a', 'tool-reply', 'tool-next'])
  );
});

/** 同样的非可听状态若来自真实静音而非SDK阶段，必须维持原整回复撤权而不能工具续说。 */
test('舞台真实静音后即使同轮新句段和新PCM到达也不解封旧reply', async () => {
  const view = await stage({ nonzeroLip: true });
  const callbacks = view.instances[0].callbacks;
  await authorizeReply(view, 1, 'muted-tool');
  view.input.audio.context.currentTime = 0.1;
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.1);
  assert.equal(callbacks.readLip()?.open, 0.87);
  view.input.peer.track.isMuted = true;
  assert.equal(callbacks.readLip(), null);
  view.input.peer.track.isMuted = false;
  await authorizeReply(view, 2, 'muted-tool', { segment_id: 'muted-tool-next' });
  view.input.audio.context.currentTime = 0.2;
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.2);
  assert.equal(callbacks.readLip(), null);
  assert.equal(callbacks.readDelivery(), null);
});

/** 动作入口不依赖音轨，但口型不能因为指令同处speaking阶段而获得额外许可。 */
test('指令在无声、无字幕的舞台可见，嘴部仍关闭；静态偏好反馈unavailable', async () => {
  const view = await stage({ nonzeroLip: true });
  const gate = view.input.delivery.current;
  const acknowledgments = [];
  gate.setAcknowledgmentSink((ack) => acknowledgments.push(ack));
  await settle(() => {
    gate.snapshot(delivery(0));
    gate.receive(
      delivery(1, {
        state: 'active',
        phase: 'speaking',
        reply_id: 'reply-a',
        segment_id: 'segment-a',
        turn_id: 'command-turn',
        gesture: 'wave',
        gesture_source: 'command',
      })
    );
  });
  assert.equal(view.instances[0].callbacks.readDelivery()?.gestureSource, 'command');
  assert.equal(view.instances[0].callbacks.readLip(), null);
  assert.equal(acknowledgments.length, 0);
  await settle(() => {
    media.matches = true;
    media.dispatchEvent(new Event('change'));
  });
  assert.equal(acknowledgments[0]?.status, 'unavailable');
  assert.ok(!acknowledgments.some((ack) => ack.status === 'completed'));
});

/** 失败提示复用原状态区，主动重试只发送同轮实时动作且不自动重放快照。 */
test('动作失败展示重试入口，发送失败可再点且不会伪造完成回执', async () => {
  const view = await stage();
  const gate = view.input.delivery.current;
  const retried = [];
  await settle(() => {
    gate.snapshot(delivery(0));
    gate.receive(
      delivery(1, {
        state: 'active',
        phase: 'idle',
        gesture: 'shake',
        gesture_source: 'command',
        turn_id: 'command-turn',
      })
    );
    gate.receive(delivery(2, { turn_id: 'command-turn', command_feedback: 'retry' }));
    view.update({
      ...view.input,
      onRetryCommand: async (gesture) => {
        retried.push(gesture);
        if (retried.length === 1) throw new Error('send failed');
      },
    });
  });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试动作' })));
  assert.ok(view.getByText('没能发出去，可以再试一次'));
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试动作' })));
  assert.deepEqual(retried, ['shake', 'shake']);
});

/** 判断超时并不授权动作；现有提示区说明降级，不能为未知轮展示上一指令的重试按钮。 */
test('未知轮表现反馈可见且不展示上一轮动作的重试入口', async () => {
  const view = await stage();
  const gate = view.input.delivery.current;
  const retried = [];
  await settle(() => {
    gate.snapshot(delivery(0));
    gate.receive(
      delivery(1, {
        state: 'active',
        phase: 'thinking',
        turn_id: 'previous-turn',
        gesture: 'wave',
        gesture_source: 'command',
      })
    );
    gate.receive(delivery(2, { turn_id: 'unknown-current-turn', command_feedback: 'retry' }));
    view.update({
      ...view.input,
      /** 仅观察真实发送入口，未知判断不得制造动作或自动重发任何用户原文。 */
      onRetryCommand: async (gesture) => retried.push(gesture),
    });
  });
  assert.ok(view.getByText('表情和动作暂时没连上，聊天可以继续，请稍后再试'));
  assert.equal(view.container.querySelector('.avatar-note').dataset.active, 'true');
  assert.equal(view.queryByRole('button', { name: '重试动作' }), null);
  assert.equal(view.container.querySelector('.avatar-media').dataset.renderer, 'live2d');
  assert.deepEqual(retried, []);
});

/** 恢复 GPU 时不能先消耗同轮新指令的开始期限；就绪后仍由用户主动点击，不补发加载期点击。 */
test('动画恢复加载期间禁用动作重试，就绪后才发送并保留重试入口', async () => {
  const loading = deferred();
  const view = await stage({ failFirst: true, loading });
  const gate = view.input.delivery.current;
  const retried = [];
  await settle(() => {
    gate.snapshot(delivery(0));
    gate.receive(
      delivery(1, {
        state: 'active',
        phase: 'idle',
        turn_id: 'command-turn',
        gesture: 'wave',
        gesture_source: 'command',
      })
    );
    gate.receive(delivery(2, { turn_id: 'command-turn', command_feedback: 'retry' }));
    view.update({
      ...view.input,
      /** 仅记录真实组件回调，加载期的按钮不能调用发送端口或产生自动补发。 */
      onRetryCommand: async (gesture) => retried.push(gesture),
    });
  });
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试动画' })));
  assert.equal(view.instances.length, 0);
  assert.ok(view.getByText('小芽正在准备，稍后可重试动作'));
  assert.equal(view.getByRole('button', { name: '重试动作' }).disabled, true);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试动作' })));
  assert.deepEqual(retried, []);
  await settle(() => loading.resolve());
  assert.equal(view.instances.length, 1);
  assert.equal(view.getByRole('button', { name: '重试动作' }).disabled, false);
  assert.deepEqual(retried, []);
  await settle(() => fireEvent.click(view.getByRole('button', { name: '重试动作' })));
  assert.deepEqual(retried, ['wave']);
});

/** jsdom 不执行媒体查询，拆开伪元素分组应用生产手机规则，验证同轮恢复入口未被隐藏。 */
test('手机紧凑布局保留失败、澄清和动作重试反馈，普通说明仍可隐藏', async () => {
  const stylesheet = document.createElement('style');
  const source = readFileSync(new URL('../styles/globals.css', import.meta.url), 'utf8');
  stylesheet.textContent = source.slice(source.indexOf('@media (max-width: 720px)'));
  document.head.append(stylesheet);
  const mobile = Array.from(stylesheet.sheet.cssRules).find(
    (rule) => rule.conditionText === '(max-width: 720px)'
  );
  assert.ok(mobile);
  stylesheet.textContent = Array.from(mobile.cssRules, (rule) =>
    rule.selectorText
      ? rule.selectorText
          .split(',')
          .map((selector) => `${selector}{${rule.style.cssText}}`)
          .join('\n')
      : rule.cssText
  ).join('\n');
  try {
    const view = await stage();
    view.container.classList.add('xiaoya-app');
    view.container.dataset.compactHeight = 'true';
    const note = view.container.querySelector('.avatar-note');
    assert.equal(getComputedStyle(note).display, 'none');
    const gate = view.input.delivery.current;
    await settle(() => {
      gate.snapshot(delivery(0));
      gate.receive(delivery(1, { command_feedback: 'clarify' }));
    });
    assert.notEqual(getComputedStyle(note).display, 'none');
    assert.match(note.textContent, /请告诉我一个具体动作/);
    await settle(() => {
      gate.receive(
        delivery(2, {
          state: 'active',
          phase: 'idle',
          turn_id: 'command-turn',
          gesture: 'wave',
          gesture_source: 'command',
        })
      );
      gate.receive(delivery(3, { turn_id: 'command-turn', command_feedback: 'retry' }));
      view.update({
        ...view.input,
        /** 本用例只检验生产反馈布局，不请求模型或构造客户端执行成功。 */
        onRetryCommand: async () => undefined,
      });
    });
    assert.notEqual(getComputedStyle(note).display, 'none');
    assert.ok(view.getByRole('button', { name: '重试动作' }));
    view.unmount();
    const failed = await stage({ failFirst: true });
    failed.container.classList.add('xiaoya-app');
    failed.container.dataset.compactHeight = 'true';
    assert.notEqual(
      getComputedStyle(failed.container.querySelector('.avatar-note')).display,
      'none'
    );
    assert.ok(failed.getByRole('button', { name: '重试动画' }));
  } finally {
    stylesheet.remove();
  }
});

/** 确认原回复真的张嘴后再注入迟到音频；停止的下一读帧即闭嘴，而不是等静音自然衰减。 */
test('倾听后迟到非零 PCM 不重新张嘴，只有匹配的新回复与实际音频可开口', async () => {
  const view = await stage({ nonzeroLip: true });
  const callbacks = view.instances[0].callbacks;
  await authorizeReply(view, 1, 'old');
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip().open, 0.87);
  // 同一渲染帧随后读取表现不会消费掉 readLip 已确认的手势。
  assert.equal(callbacks.readDelivery().gesture, 'nod');
  assert.equal(callbacks.readDelivery().gesture, 'nod');
  view.input = { ...view.input, peer: { ...view.input.peer, state: 'listening' } };
  await settle(() => view.update(view.input));
  for (let frame = 1; frame <= 20; frame++) {
    view.input.audio.context.currentTime = frame / 60;
    view.bridges[0].config.onSamples(
      new Float32Array([0.5]),
      24000,
      view.input.audio.context.currentTime
    );
    assert.equal(callbacks.readLip(), null);
  }
  // SDK speaking 迟到及更高视觉修订都不能复活已经被倾听截断的旧回复。
  view.input = { ...view.input, peer: { ...view.input.peer, state: 'speaking' } };
  await authorizeReply(view, 2, 'old');
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.4);
  view.input.audio.context.currentTime = 0.4;
  assert.equal(callbacks.readLip(), null);
  assert.equal(callbacks.readDelivery(), null);
  // 新身份解除旧回复栅栏；停止阶段丢弃的音频不能被拿来给新回复张嘴。
  await authorizeReply(view, 3, 'new');
  // 保持同一音轨与尚未过期的时间，不能靠推进时钟让旧口型自然过期来通过。
  assert.equal(callbacks.readLip(), null);
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0.4);
  assert.equal(callbacks.readLip().open, 0.87);
  assert.equal(callbacks.readDelivery().gesture, 'nod');
});

/** 用户取消、换轨和重连都通过同一真实组件与 gate，旧队列不能在连接恢复时补演。 */
test('取消、换轨及恢复拒绝迟到口型，播放受阻或音轨结束下一帧闭嘴', async () => {
  const view = await stage({ nonzeroLip: true });
  const callbacks = view.instances[0].callbacks;
  await authorizeReply(view, 1, 'first');
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip().open, 0.87);
  view.input.delivery.current.interruptSpeaking();
  view.bridges[0].config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip(), null);
  const nextTrack = new RemoteTrack();
  view.input = { ...view.input, peer: { ...view.input.peer, track: nextTrack } };
  await settle(() => view.update(view.input));
  view.bridges.at(-1).config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip(), null);
  await authorizeReply(view, 2, 'second');
  view.bridges.at(-1).config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip().open, 0.87);
  view.input = { ...view.input, reconnecting: true };
  await settle(() => view.update(view.input));
  assert.equal(callbacks.readLip(), null);
  view.input = { ...view.input, reconnecting: false };
  await settle(() => view.update(view.input));
  view.bridges.at(-1).config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip(), null);
  await authorizeReply(view, 3, 'third');
  view.bridges.at(-1).config.onSamples(new Float32Array([0.5]), 24000, 0);
  assert.equal(callbacks.readLip().open, 0.87);
  let revision = 3;
  for (const change of [
    /** 公开播放许可丢失不能让旧非零分析帧继续占有嘴部。 */
    () => {
      view.input.room.canPlaybackAudio = false;
    },
    /** 静音音轨即使仍存在，也不代表用户有实际声音出口。 */
    () => {
      view.input.peer.track.isMuted = true;
    },
    /** 停止的 native 轨道可能先于 SDK 观察更新，帧级读取必须检查真实终态。 */
    () => {
      view.input.peer.track.mediaStreamTrack.readyState = 'ended';
    },
    /** 暂停的播放时钟不能借用尚未过期的已有开口帧。 */
    () => {
      view.input.audio.context.state = 'suspended';
    },
  ]) {
    view.input.room.canPlaybackAudio = true;
    view.input.peer.track = new RemoteTrack();
    view.input.audio.context.state = 'running';
    await settle(() => view.update(view.input));
    await authorizeReply(view, ++revision, `permission-${revision}`);
    view.bridges.at(-1).config.onSamples(new Float32Array([0.5]), 24000, 0);
    assert.equal(callbacks.readLip().open, 0.87);
    change();
    view.bridges.at(-1).config.onSamples(new Float32Array([0.5]), 24000, 0);
    assert.equal(callbacks.readLip(), null);
  }
});
