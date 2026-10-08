import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { RemoteTrack, conversationPorts } from './helpers/conversation-ports.mjs';
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
    '@/lib/avatar/audio-bridge': { AvatarAudioBridge: Bridge },
    '@/lib/avatar/sdk-loader': { loadSdkScript: async () => undefined },
    '@/lib/avatar/motion-sync': {
      /** 官方分析库属于外部 native 端口，测试只验证它的所有权和失效边界。 */
      createMotionSyncAnalyzer: async () => {
        const analyzer = {
          mode: 'motionsync',
          disposed: 0,
          sample: () => [],
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
