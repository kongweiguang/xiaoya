import assert from 'node:assert/strict';
import { test } from 'node:test';
import { advanceFrameDeadline, easeParameter, selectBehavior } from '../lib/avatar/behavior.ts';

/** 在144Hz每次等满16.7ms会退化为48fps，必须验证调度保留取整余数。 */
test('60 fps 调度在 60、144、165 Hz 下均不因取整降低帧率', () => {
  for (const refresh of [60, 144, 165]) {
    let deadline = 0;
    let drawn = 0;
    for (let step = 0; step < refresh * 10; step++) {
      const time = step * (1000 / refresh);
      if (time + 0.5 >= deadline) {
        drawn++;
        deadline = advanceFrameDeadline(deadline, time, 1000 / 60);
      }
    }
    assert.ok(drawn >= 599 && drawn <= 601, `${refresh} Hz: ${drawn}`);
  }
});

/** 后台暂停不能追补数千帧，恢复后只渲染当前姿态。 */
test('调度在长暂停后恢复下一个截止点', () => {
  const next = advanceFrameDeadline(100, 30_100, 1000 / 60);
  assert.ok(next > 30_100 && next <= 30_100 + 1000 / 60 + 1e-8);
});

/** SDK 的迟到说话状态不能让已断开的舞台继续表演，错误和恢复状态需要先向用户反馈。 */
test('连接与错误优先于迟到的说话状态，恢复后才回到正常表情', () => {
  const lateSpeaking = { connected: false, reconnecting: false, error: false, agent: 'speaking' };
  assert.equal(selectBehavior(lateSpeaking), 'idle');
  assert.equal(selectBehavior({ ...lateSpeaking, agent: 'thinking' }), 'idle');
  assert.equal(selectBehavior({ ...lateSpeaking, error: true }), 'confused');
  assert.equal(selectBehavior({ ...lateSpeaking, reconnecting: true }), 'confused');
  assert.equal(
    selectBehavior({ ...lateSpeaking, connected: true, reconnecting: true }),
    'confused'
  );
  assert.equal(selectBehavior({ ...lateSpeaking, connected: true, error: true }), 'confused');
  assert.equal(selectBehavior({ ...lateSpeaking, connected: true }), 'speaking');
});

/** 表情只描述对话神态，不携带口型；初始化与未知 SDK 状态也必须有确定的可用表现。 */
test('已连接会话区分倾听、思考与说话，未知状态保留倾听', () => {
  const connected = { connected: true, reconnecting: false, error: false, agent: 'listening' };
  assert.equal(selectBehavior(connected), 'listening');
  assert.equal(selectBehavior({ ...connected, agent: 'thinking' }), 'thinking');
  assert.equal(selectBehavior({ ...connected, agent: 'speaking' }), 'speaking');
  assert.equal(selectBehavior({ ...connected, agent: 'initializing' }), 'listening');
  assert.equal(selectBehavior({ ...connected, agent: 'future-sdk-state' }), 'listening');
});

/** 保证不同设备的刷新率得到相同的动作速度，避免降为 30 fps 后人物明显迟钝。 */
test('30 fps 与 60 fps 在相同播放时长达到相同姿态', () => {
  let at30 = -4;
  let at60 = -4;
  for (let frame = 0; frame < 30; frame += 1) at30 = easeParameter(at30, 6, 1 / 30);
  for (let frame = 0; frame < 60; frame += 1) at60 = easeParameter(at60, 6, 1 / 60);
  assert.ok(Math.abs(at30 - at60) < 1e-12);
  assert.ok(at30 > 5.99 && at30 < 6);
});

/** 姿态变换必须单调接近目标，收敛时不越界；负方向的头部和手臂也遵守同一约束。 */
test('过渡不会过冲，反向姿态同样平滑，零时间保持原值', () => {
  assert.equal(easeParameter(2, -7, 0), 2);
  assert.equal(easeParameter(3, 3, 1 / 60), 3);
  let current = 2;
  for (let frame = 0; frame < 120; frame += 1) {
    const next = easeParameter(current, -7, 1 / 60);
    assert.ok(next >= -7 && next <= current);
    current = next;
  }
  assert.ok(Math.abs(current + 7) < 1e-6);
});

/** 后台停留十分钟也不能在恢复首帧瞬移到新表情，否则恢复连接会产生可见跳变。 */
test('长时间后台恢复的首步有界，仍保留后续平滑过渡', () => {
  const afterPause = easeParameter(-4, 6, 600);
  assert.ok(afterPause > -4 && afterPause < 0);
  const next = easeParameter(afterPause, 6, 1 / 60);
  assert.ok(next > afterPause && next < 6);
});
