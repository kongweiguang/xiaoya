import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { DeliveryGate } from '../lib/avatar/delivery.ts';
import { conversationPorts, delivery, events } from './helpers/conversation-ports.mjs';
import { cleanup, production, renderHook, settle } from './helpers/dom.mjs';

afterEach(cleanup);

/** 测试实际 hook 的提交与清理，网络同步由独立生产通道测试覆盖。 */
async function hook(initial = {}) {
  const { useAvatarDelivery } = await production('hooks/use-avatar-delivery.ts');
  const active = delivery(1, {
    state: 'active',
    reply_id: 'reply-a',
    segment_id: 'segment-a',
    style: 'happy',
    gesture: 'wave',
  });
  const messages = [
    {
      id: active.segment_id,
      message: '你好。',
      from: { identity: 'agent', isLocal: false },
      attributes: { 'xiaoya.delivery': JSON.stringify(active), 'lk.segment_id': active.segment_id },
    },
  ];
  const gate = new DeliveryGate();
  gate.snapshot(active);
  const input = {
    gate: { current: gate },
    messages,
    identity: 'agent',
    state: 'speaking',
    track: {},
    ...initial,
  };
  const view = renderHook(
    (props) =>
      useAvatarDelivery(props.gate, props.messages, props.identity, props.state, props.track),
    { initialProps: input }
  );
  return { ...view, input, gate, active };
}

/** 本地字幕即使先于服务器 closed 包也要截断旧回复，之后的 speaking 不会重新激活它。 */
test('新本地输入及离开 speaking 立即停止旧表现', async () => {
  const view = await hook();
  assert.ok(view.gate.read(true, true));
  view.rerender({
    ...view.input,
    messages: [
      ...view.input.messages,
      { id: 'local-a', message: '打断', from: { identity: 'user', isLocal: true } },
    ],
  });
  assert.equal(view.gate.read(true, true), null);
  view.rerender({ ...view.input, state: 'listening' });
  view.rerender(view.input);
  assert.equal(view.gate.read(true, true), null);
});

/** 首轨不是打断，同轨重渲染不误取消；真正换轨则不能继续用旧音频的动作。 */
test('音轨替换停止当前动作但同轨更新保持许可', async () => {
  const view = await hook();
  assert.ok(view.gate.read(true, true));
  view.rerender({ ...view.input });
  assert.ok(view.gate.read(true, true));
  view.rerender({ ...view.input, track: {} });
  assert.equal(view.gate.read(true, true), null);
});

for (const kind of ['playback', 'visibility']) {
  /** 失效和恢复来自公开能力事件，真实通道负责取消受阻期间的回复而非 hook 复制规则。 */
  test(`${kind} 恢复不补演受阻期间的新回复，重复事件不取消后续回复`, async () => {
    const environment = conversationPorts();
    const room = new environment.ports['livekit-client'].Room({});
    await room.start();
    const { DeliveryChannel } = await production('lib/delivery-channel.ts', environment.ports);
    const channel = new DeliveryChannel(
      room,
      environment.agent,
      new DeliveryGate(),
      () => undefined
    );
    try {
      assert.equal(await channel.ready, true);
      /** 字幕和可靠包同步到真实 gate，只有表现失效来自浏览器能力变化。 */
      const publish = (revision, reply) => {
        const state = delivery(revision, {
          state: 'active',
          reply_id: reply,
          segment_id: 'segment-' + revision,
          style: 'happy',
          gesture: 'wave',
        });
        channel.offer([
          {
            id: state.segment_id,
            message: '你好。',
            from: { identity: 'agent', isLocal: false },
            attributes: {
              'xiaoya.delivery': JSON.stringify(state),
              'lk.segment_id': state.segment_id,
            },
          },
        ]);
        room.emit(
          events.DataReceived,
          new TextEncoder().encode(JSON.stringify(state)),
          environment.agent,
          0,
          'xiaoya.delivery'
        );
      };
      /** 后台与播放许可共享回复失效边界，测试不直接修改生产门控。 */
      const available = (value) => {
        if (kind === 'visibility') {
          Object.defineProperty(document, 'hidden', { value: !value, configurable: true });
          document.dispatchEvent(new Event('visibilitychange'));
        } else {
          room.canPlaybackAudio = value;
          room.emit(events.AudioPlaybackStatusChanged);
        }
      };
      publish(1, 'reply-a');
      assert.ok(channel.gate.read(true, true));
      available(false);
      publish(2, 'reply-blocked');
      available(true);
      assert.equal(channel.gate.read(true, true), null);
      publish(3, 'reply-blocked');
      assert.equal(channel.gate.read(true, true), null);
      publish(4, 'reply-new');
      assert.ok(channel.gate.read(true, true));
      available(true);
      assert.ok(channel.gate.read(true, true));
    } finally {
      channel.close();
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
    }
    await settle();
  });
}
