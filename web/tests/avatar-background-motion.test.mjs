import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AvatarPresentation } from '../lib/avatar/presentation.ts';

/** 复用07真实失败的阶段时刻，音频/视线采用明确有界输入而不冒称录屏没有保存的精确gaze。 */
function quietReplyTrace({ fps = 60, gazeX = -0.225, energy = 0.75 } = {}) {
  const presentation = new AvatarPresentation();
  const rows = [];
  const origin = 180.7077;
  for (let frame = -fps; frame <= 8 * fps; frame++) {
    const age = frame / fps;
    const time = origin + age;
    const thinking = age >= 0.0431 && age < 1.5373;
    const speaking = age >= 1.5373 && age < 3.9794;
    const activePcm = age >= 1.6624 && age <= 4.0625;
    const phase = thinking ? 'thinking' : speaking ? 'speaking' : 'listening';
    const audioTick = Math.floor(Math.max(0, age - 1.6624) / 0.02);
    const delivery = {
      id: `quiet-${phase}`,
      instance: 'job-quiet',
      replyKey: 'quiet-reply',
      turnId: age < 0.0431 ? 'previous-turn' : 'quiet-turn',
      segmentKey: speaking ? 'quiet-segment' : '',
      style: 'neutral',
      gesture: 'none',
      gestureId: '',
      gestureSource: 'none',
      phase,
      energy: 0.5,
      audio: activePcm
        ? { at: audioTick, energy, onset: audioTick % 32 === 0 ? 0.6 : 0 }
        : undefined,
    };
    const parameters = presentation.advance({
      time,
      delta: frame === -fps ? 0 : 1 / fps,
      behavior: phase,
      delivery,
      gaze: { x: gazeX, y: 0.1 },
      blink: 1,
      lip: activePcm ? { open: energy, form: 0.2 } : null,
    });
    if (age >= 0)
      rows.push({ age, parameters, acknowledgments: presentation.drainAcknowledgments() });
  }
  return rows;
}

/** 原房间禁止场景用Xmin<-2/Xmax>4/span>5判摇头；普通状态切换必须在该原门之下。 */
test('普通思考、视线和PCM节奏不能合成禁止的摇头', () => {
  for (const fps of [30, 60]) {
    const rows = quietReplyTrace({ fps });
    const x = rows.map((row) => row.parameters.ParamAngleX);
    const min = Math.min(...x),
      max = Math.max(...x);
    assert.equal(min < -2 && max > 4 && max - min > 5, false, JSON.stringify({ fps, min, max }));
    assert.ok(max < 4, '无语义动作时，普通思考不达到原摇头的正向幅度');
    assert.ok(rows.every((row) => row.acknowledgments.length === 0));
    assert.ok(
      rows.every((row) => row.parameters.ParamArmL === 0 && row.parameters.ParamArmR === 0)
    );
    assert.ok(rows.at(-1).parameters.ParamMouthOpenY === 0);
  }
});

/** 接口极端视线与0/.45/1音频能量也保持微动；眼球全幅、嘴部PCM独占和有限参数不受缩小头幅影响。 */
test('普通微动在30与60fps的极端视线和能量下有界且保持口型独立', () => {
  for (const fps of [30, 60]) {
    for (const gazeX of [-1, 0, 1]) {
      for (const energy of [0, 0.45, 1]) {
        const rows = quietReplyTrace({ fps, gazeX, energy });
        const x = rows.map((row) => row.parameters.ParamAngleX);
        const min = Math.min(...x),
          max = Math.max(...x);
        assert.equal(
          min < -2 && max > 4 && max - min > 5,
          false,
          JSON.stringify({ fps, gazeX, energy, min, max })
        );
        assert.ok(max < 4 && min > -4);
        for (const row of rows) {
          assert.ok(Object.values(row.parameters).every(Number.isFinite));
          assert.equal(
            row.parameters.ParamMouthOpenY,
            row.age >= 1.6624 && row.age <= 4.0625 ? energy : 0
          );
        }
        const lastSpeaking = rows.findLast((row) => row.age < 3.9794 && row.age > 3.9);
        assert.ok(Math.abs(lastSpeaking.parameters.ParamEyeBallX - gazeX) < 1e-10);
        assert.ok(Math.abs(rows.at(-1).parameters.ParamEyeBallX) < 1e-10);
      }
    }
  }
});
