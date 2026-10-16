import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AvatarPresentation } from '../lib/avatar/presentation.ts';

/** 复用实际MIC14的同轮身份关系，测试只供给输入而不替生产类实现动作或冷却。 */
function cue(id, gesture = 'nod', overrides = {}) {
  return {
    instance: 'mic14',
    id,
    replyKey: 'same-reply',
    style: 'neutral',
    gesture,
    phase: 'speaking',
    turnId: 'same-turn',
    segmentKey: id,
    gestureId: id,
    gestureSource: gesture === 'none' ? 'none' : 'dialogue',
    energy: 0.5,
    ...overrides,
  };
}

/** 真实PCM通道与嘴部独立传入，队列不得把单有口型或字幕误当可发力音频。 */
function frame(runtime, time, delivery, { fps = 60, audio = true, lip = 0.6 } = {}) {
  const received =
    delivery && delivery.phase === 'speaking' && audio
      ? { ...delivery, audio: { at: time, energy: 0.55, onset: 0.12 } }
      : delivery;
  const pose = runtime.advance({
    time,
    delta: 1 / fps,
    behavior: delivery?.phase ?? 'idle',
    delivery: received,
    gaze: { x: 0.1, y: 0 },
    blink: 1,
    lip: delivery?.phase === 'speaking' ? { open: lip, form: -0.2 } : null,
  });
  assert.equal(pose.ParamMouthOpenY, delivery?.phase === 'speaking' ? lip : 0);
  assert.equal(pose.ParamMouthForm, delivery?.phase === 'speaking' ? -0.2 : 0);
  return pose;
}

/** 起声前的真实倾听点头会消耗原6秒预算，后续说话候选必须等待而不是覆盖或吞掉。 */
function listeningSeed(runtime, fps = 60) {
  const listening = cue('listening-nod', 'nod', { phase: 'listening', segmentKey: '' });
  for (let tick = 0; tick <= 2 * fps; tick++)
    frame(runtime, tick / fps, tick < fps ? listening : null, { fps });
  return runtime.naturalStarts[0];
}

/** 原失败有充足同句PCM；回执标明实际原stroke，不改变同类冷却与原幅度，候选不能因已见ID漏执行。 */
test('MIC14倾听后同类说话候选在原6秒冷却后执行一次且原nod幅度保持', () => {
  for (const fps of [30, 60]) {
    const runtime = new AvatarPresentation();
    const started = listeningSeed(runtime, fps);
    // 实测28370.6→32039.5、首PCM32313.124；仅平移时钟，不缩短6秒门或延长当前句。
    const targetAt = 3.6689;
    const pcmAt = targetAt + (32313.124 - 32039.5) / 1000;
    const endAt = targetAt + (38365.4 - 32039.5) / 1000;
    const speaking = cue('speaking-nod');
    let low = Infinity;
    let high = -Infinity;
    for (let tick = 2 * fps + 1; tick <= Math.ceil((endAt + 1) * fps); tick++) {
      const time = tick / fps;
      const active = time >= targetAt && time < endAt;
      const pose = frame(runtime, time, active ? speaking : null, {
        fps,
        audio: time >= pcmAt,
        lip: time >= pcmAt ? 0.6 : 0,
      });
      if (active && time >= started + 6) {
        low = Math.min(low, pose.ParamAngleY);
        high = Math.max(high, pose.ParamAngleY);
      }
      if (time < started + 6) assert.equal(runtime.naturalStarts.length, 1);
    }
    assert.equal(runtime.naturalStarts.length, 2);
    assert.ok(runtime.naturalStarts[1] - started >= 6);
    assert.ok(high - low > 8 && low < -2.5, JSON.stringify({ fps, low, high }));
    assert.deepEqual(
      runtime.drainAcknowledgments().map((ack) => [ack.gesture_id, ack.status]),
      [
        ['listening-nod', 'started'],
        ['listening-nod', 'canceled'],
        ['speaking-nod', 'started'],
        ['speaking-nod', 'completed'],
      ]
    );
  }
});

/** 队列只有当前许可，不能把已静音的字幕或单独嘴部输入当作实际PCM并在后续补演。 */
test('冷却结束后仍无实际PCM的自然候选不开始，真实PCM到来才准备', () => {
  const runtime = new AvatarPresentation();
  listeningSeed(runtime);
  const speaking = cue('waiting-audio');
  for (let tick = 121; tick <= 8 * 60; tick++) {
    const time = tick / 60;
    frame(runtime, time, time < 3.7 ? null : speaking, { audio: time >= 7.2 });
    if (time < 7.2) assert.equal(runtime.naturalStarts.length, 1);
  }
  assert.equal(runtime.naturalStarts.length, 2);
  assert.ok(runtime.naturalStarts[1] >= 7.32);
});

/** 所有撤权路径均在原冷却到期前离开当前身份，避免隐藏、断线或新轮使旧结果复活。 */
test('等待期间none、换段、换ID、新轮、连接变更、null和cancel均撤去旧自然候选', () => {
  const removals = {
    // 同句none明确撤去尚未执行的动作许可。
    none: (old) => ({ ...old, id: 'none', gesture: 'none', gestureId: '', gestureSource: 'none' }),
    // 新句不能借上句的等待动作，即使回复与用户轮次相同。
    segment: (old) => ({
      ...old,
      id: 'new-segment',
      segmentKey: 'other',
      gesture: 'none',
      gestureId: '',
      gestureSource: 'none',
    }),
    // 不同一次执行身份不能接管旧候选的许可。
    gestureId: (old) => ({
      ...old,
      id: 'new-gesture-id',
      gestureId: 'other-gesture',
      gesture: 'none',
      gestureSource: 'none',
    }),
    // 相同回复标签不能越过真实用户轮次边界。
    turn: (old) => ({
      ...old,
      id: 'new-turn',
      turnId: 'other-turn',
      gesture: 'none',
      gestureId: '',
      gestureSource: 'none',
    }),
    // 重连的新拥有者不能恢复旧连接的等待动作。
    instance: (old) => ({
      ...old,
      id: 'new-instance',
      instance: 'other-job',
      gesture: 'none',
      gestureId: '',
      gestureSource: 'none',
    }),
    // 舞台隐藏或连接失效由现有Gate返回null，不引入另一套可见性协议。
    hiddenOrDisconnected: () => null,
    // 显式取消后即使同份delivery仍出现，也不能重新采纳。
    cancel: (old) => old,
  };
  for (const [reason, remove] of Object.entries(removals)) {
    const runtime = new AvatarPresentation();
    listeningSeed(runtime);
    const speaking = cue('revoked');
    for (let tick = 121; tick <= 9 * 60; tick++) {
      const time = tick / 60;
      const delivery = time < 3.7 ? null : time < 4.5 ? speaking : remove(speaking);
      if (reason === 'cancel' && tick === 4.5 * 60) runtime.cancel();
      frame(runtime, time, delivery);
    }
    assert.equal(runtime.naturalStarts.length, 1, reason);
    assert.equal(runtime.pendingNatural, null, reason);
  }
});

/** 新的当前候选只替换一个等待槽；不回放上段动作，也不因为版本更新重复同一动作。 */
test('等待槽只保存当前同句单候选，换身份只执行新候选一次', () => {
  const runtime = new AvatarPresentation();
  listeningSeed(runtime);
  const old = cue('old-candidate');
  const current = cue('new-candidate', 'nod', { segmentKey: 'new-segment' });
  for (let tick = 121; tick <= 10 * 60; tick++) {
    const time = tick / 60;
    const delivery = time < 3.7 ? null : time < 5 ? old : { ...current, id: `version-${tick}` };
    frame(runtime, time, delivery);
    if (runtime.activeGesture)
      assert.notEqual(runtime.activeGesture.delivery.gestureId, old.gestureId);
  }
  assert.equal(runtime.naturalStarts.length, 2);
  assert.equal(runtime.pendingNatural, null);
});

/** 10秒两次与3秒间隔保持；指令回执独立筛选，新增自然回执不能误当重复执行指令。 */
test('待执行自然候选仍守3秒与10秒两次限制，指令抢占会清掉等待槽', () => {
  const runtime = new AvatarPresentation();
  const schedule = [
    [0, cue('first', 'nod', { phase: 'listening' })],
    [3.5, cue('second', 'tilt', { phase: 'listening' })],
    [6.8, cue('third', 'wave', { phase: 'listening' })],
  ];
  const starts = [];
  for (let tick = 0; tick <= 12 * 60; tick++) {
    const time = tick / 60;
    const delivery = schedule.findLast(([at]) => time >= at)[1];
    frame(runtime, time, delivery);
    if (runtime.activeGesture?.stroke === time) starts.push(time);
    if (time < 10) assert.ok(starts.length <= 2);
  }
  assert.equal(starts.length, 3);
  assert.ok(starts[1] - starts[0] >= 3 && starts[2] - starts[1] >= 3);
  assert.ok(starts[2] - starts[0] >= 10);

  const interrupted = new AvatarPresentation();
  listeningSeed(interrupted);
  const pending = cue('pending');
  const command = cue('explicit', 'wave', { gestureSource: 'command', phase: 'idle' });
  const acks = [];
  for (let tick = 121; tick <= 9 * 60; tick++) {
    const time = tick / 60;
    frame(interrupted, time, time < 3.7 ? null : time < 4.5 ? pending : command);
    acks.push(...interrupted.drainAcknowledgments());
  }
  assert.deepEqual(
    acks.filter((ack) => ack.gesture_id === 'explicit').map((ack) => ack.status),
    ['started', 'completed']
  );
  assert.equal(interrupted.naturalStarts.length, 1);
  assert.equal(interrupted.pendingNatural, null);
});
