import assert from 'node:assert/strict';
import { test } from 'node:test';
import { AvatarPresentation } from '../lib/avatar/presentation.ts';

// 来自封口MIC13问候首句的观察渲染时刻、实际PCM桥包络及已观察口型；不是固定声能的理想正弦。
// 原report SHA40f81b0b…298269、原presentation b83913b5…439a23；原draw精确时刻未存，使用变化后首次观察。
const samples = [
  [16277.4, 15.340666667, 0, 0, 0],
  [16298.3, 15.360666667, 0, 0, 0],
  [16311.8, 15.380666667, 0, 0, 0],
  [16325.9, 15.400666667, 0, 0, 0],
  [16346.7, 15.420666667, 0, 0, 0],
  [16360.8, 15.420666667, 0, 0, 0],
  [16381.2, 15.440666667, 0, 0, 0],
  [16395.2, 15.460666667, 0, 0, 0],
  [16409, 15.480666667, 0, 0, 0],
  [16430, 15.500666667, 0, 0, 0],
  [16443.7, 15.520666667, 0, 0, 0],
  [16464.9, 15.540666667, 0, 0, 0],
  [16478.5, 15.540666667, 0, 0, 0],
  [16492.3, 15.560666667, 0, 0, 0],
  [16513.2, 15.580666667, 0, 0, 0],
  [16527.3, 15.600666667, 0, 0, 0],
  [16548.2, 15.620666667, 0, 0, 0],
  [16561.7, 15.620666667, 0, 0, 0],
  [16582.8, 15.660666667, 0, 0, 0],
  [16596.9, 15.660666667, 0, 0, 0],
  [16610.9, 15.680666667, 0.20052221, 0.398324109, 0.047909878],
  [16631.4, 15.700666667, 0.472539921, 0.342543293, 0.503509939],
  [16652.3, 15.720666667, 0.6283153, 0.041110006, 0.649725318],
  [16659.2, 15.720666667, 0.6283153, 0.041110006, 0.655327559],
  [16680.2, 15.740666667, 0.607901379, 0, 0.655943811],
  [16694, 15.760666667, 0.47932648, 0, 0.655949414],
  [16714.7, 15.780666667, 0.343054898, 0, 0.652977943],
  [16728.7, 15.800666667, 0.25570321, 0.025541393, 0.658330262],
  [16749.4, 15.820666667, 0.202465533, 0.025487677, 0.658919036],
  [16763.5, 15.840666667, 0.383394536, 0.482260786, 0.658924401],
  [16784.4, 15.860666667, 0.566498959, 0.18525033, 0.658924997],
  [16798, 15.860666667, 0.566498959, 0.18525033, 0.655712008],
  [16811.9, 15.880666667, 0.588113231, 0, 0.68002373],
  [16826.1, 15.900666667, 0.532180147, 0, 0.699833989],
  [16846.7, 15.920666667, 0.463237156, 0, 0.700567603],
  [16860.3, 15.920666667, 0.463237156, 0, 0.700574279],
  [16881.2, 15.940666667, 0.404110128, 0, 0.703252494],
  [16895.2, 15.960666667, 0.375832274, 0.042913192, 0.905520439],
  [16909, 15.980666667, 0.458595793, 0.229661434, 0.92503202],
  [16930.3, 16.000666667, 0.633004431, 0.264810488, 0.922894299],
  [16943.7, 16.020666667, 0.790667776, 0.14114522, 0.926162422],
  [16964.9, 16.040666667, 0.723411443, 0, 0.930402696],
  [16978.7, 16.040666667, 0.723411443, 0, 0.93211025],
  [17000.1, 16.060666667, 0.618596707, 0, 0.957349598],
  [17013.4, 16.080666667, 0.486408193, 0, 0.92246145],
  [17027.3, 16.100666667, 0.376842666, 0, 0.784346163],
  [17048.3, 16.120666667, 0.341984017, 0.1375434, 0.74344939],
  [17062.1, 16.140666667, 0.456407752, 0.307738945, 0.734314919],
  [17076.1, 16.140666667, 0.456407752, 0.307738945, 0.702786624],
  [17096.8, 16.160666667, 0.702600777, 0.376174714, 0.654008567],
  [17110.8, 16.180666667, 0.852315916, 0.054546086, 0.650400817],
  [17131.4, 16.200666667, 0.926662255, 0, 0.65268147],
  [17145.5, 16.220666667, 0.942052944, 0, 0.655917346],
  [17166.1, 16.240666667, 0.795889481, 0, 0.687544167],
  [17180.2, 16.240666667, 0.795889481, 0, 0.699271917],
  [17193.6, 16.260666667, 0.555273249, 0, 0],
  [17214.9, 16.280666667, 0.387401, 0, 0],
  [17228.6, 16.300666667, 0.270280507, 0, 0],
  [17242.9, 16.320666667, 0.188568311, 0, 0],
  [17263.1, 16.340666667, 0.131559646, 0, 0],
  [17277.2, 16.340666667, 0.131559646, 0, 0],
  [17298.2, 16.360666667, 0.091786051, 0, 0],
  [17311.9, 16.380666667, 0.064036955, 0, 0],
  [17326, 16.400666667, 0.044677067, 0, 0],
  [17346.5, 16.420666667, 0.031170132, 0, 0],
  [17360.4, 16.420666667, 0.031170132, 0, 0],
  [17381.3, 16.440666667, 0.021746663, 0, 0],
  [17395.3, 16.460666667, 0.015172132, 0, 0],
  [17416.4, 16.480666667, 0.010585237, 0, 0],
  [17430.2, 16.500666667, 0.00738507, 0, 0],
  [17443.9, 16.520666667, 0.005152388, 0, 0],
  [17464.5, 16.540666667, 0.003594699, 0, 0],
  [17478.8, 16.540666667, 0.003594699, 0, 0],
  [17499.5, 16.560666667, 0.002507937, 0, 0],
  [17513.6, 16.580666667, 0.001749728, 0, 0],
  [17527.1, 16.600666667, 0.001220744, 0, 0],
  [17547.9, 16.620666667, 0.000851684, 0, 0],
  [17562.1, 16.640666667, 0.0005942, 0, 0],
  [17582.7, 16.660666667, 0.000414559, 0, 0],
  [17596.8, 16.660666667, 0.000414559, 0, 0],
  [17617.5, 16.680666667, 0.000289228, 0, 0],
  [17631.3, 16.700666667, 0.000201788, 0, 0],
  [17652.3, 16.720666667, 0.046274708, 0.091722596, 0.0],
  [17659.4, 16.720666667, 0.046274708, 0.091722596, 0.0],
  [17680.2, 16.740666667, 0.101758942, 0.064767874, 0.0],
  [17693.6, 16.760666667, 0.152787699, 0.046633721, 0.094324909],
  [17714.8, 16.780666667, 0.20088718, 0.045209946, 0.381902009],
  [17728.7, 16.800666667, 0.264344469, 0.078606749, 0.421140164],
  [17742.6, 16.820666667, 0.390578859, 0.188159849, 0.411294013],
  [17763.1, 16.840666667, 0.657735073, 0.406166273, 0.765994012],
  [17777.2, 16.840666667, 0.657735073, 0.406166273, 0.85599941],
  [17798.2, 16.860666667, 0.748535413, 0, 0.844862461],
  [17811.8, 16.880666667, 0.688712324, 0, 0.860061228],
  [17832.8, 16.900666667, 0.615520932, 0, 0.878563106],
  [17853.6, 16.920666667, 0.537431429, 0, 0.900550604],
  [17860.6, 16.920666667, 0.537431429, 0, 0.906175077],
  [17881.2, 16.940666667, 0.412575433, 0, 0.967876732],
  [17895.2, 16.960666667, 0.297293738, 0, 0.985220253],
  [17909.1, 16.980666667, 0.223600419, 0.022280727, 0.985517025],
  [17930.2, 17.000666667, 0.16802669, 0, 0.933147252],
];
const lateCandidateAt = 16716.6;
const firstLegalPcmRenderAt = 16610.9;
const segmentEndAt = 17243.4;
const originalLateWindowMs = 526.8;

/** 只生成生产协议已有的稳定身份，晚到与预判共享同一真实句段边界，不借用下一句延长动作。 */
function delivery(gesture = 'wave', phase = 'speaking', source = 'dialogue') {
  return {
    instance: '37e4a9b8a1ea44a28e27c305a62d0b79',
    id: `revision:${gesture}:${phase}`,
    replyKey: 'speech_76008be7df47',
    segmentKey: 'SG_03a3a8776c724fc9a947a39673f73321',
    turnId: 'b27e5ad0720e48c1969b29604237d9f2',
    gestureId:
      gesture === 'none' ? '' : 'speech_76008be7df47:SG_03a3a8776c724fc9a947a39673f73321:wave',
    gestureSource: gesture === 'none' ? 'none' : source,
    gesture,
    phase,
    style: 'happy',
    energy: 0.51,
  };
}

/** 真实短窗由生产类发力和撤权；自然回执必须绑定本身份，不能用理想PCM或指令回执掩盖迟峰。 */
function replay(targetAt, cutoffAt) {
  const runtime = new AvatarPresentation();
  const trace = [];
  const acks = [];
  let previousAt = null;
  for (const [at, audioAt, energy, onset, open] of samples) {
    const active = at >= targetAt && at < cutoffAt;
    const speaking = at < cutoffAt;
    const cue = speaking ? delivery(active ? 'wave' : 'none') : null;
    const output = runtime.advance({
      time: at / 1000,
      delta: previousAt === null ? 1 / 60 : (at - previousAt) / 1000,
      behavior: speaking ? 'speaking' : 'idle',
      delivery: cue ? { ...cue, audio: { at: audioAt, energy, onset } } : null,
      gaze: { x: 0, y: 0 },
      blink: 1,
      lip: { open, form: 0 },
    });
    previousAt = at;
    assert.equal(output.ParamMouthOpenY, open, '语义时间包络不能触碰实际PCM嘴部');
    assert.equal(output.ParamMouthForm, 0);
    assert.ok(Object.values(output).every(Number.isFinite));
    acks.push(...runtime.drainAcknowledgments());
    trace.push({ at, output, active });
  }
  assert.deepEqual(
    acks.map((ack) => [ack.gesture_id, ack.status]),
    [
      [delivery('wave').gestureId, 'started'],
      [delivery('wave').gestureId, 'canceled'],
    ]
  );
  return trace;
}

/** 撤权后的目标立即归零而姿态经原阻尼收回，首帧不能瞬移归位，也不得借后一句重新抬臂。 */
function assertSmoothReturn(trace, cutoffAt) {
  const active = trace.filter((row) => row.active);
  const tail = trace.filter((row) => row.at >= cutoffAt);
  const peak = Math.max(...active.map((row) => row.output.ParamArmL));
  assert.ok(tail[0].output.ParamArmL > 0, '取消首帧保留平滑回收姿态');
  assert.ok(Math.abs(tail[0].output.ParamArmL - active.at(-1).output.ParamArmL) < 0.04);
  assert.ok(tail.every((row) => row.output.ParamArmL <= peak + 0.005));
  assert.ok(tail.find((row) => row.at >= cutoffAt + 250).output.ParamArmL < 0.01);
  assert.ok(tail.find((row) => row.at >= cutoffAt + 600).output.ParamArmL < 0.00003);
}

/** 原526.8ms晚到样本曾仅抬臂.1148；保留原>.16门，缩短自然wave后须在同句撤权前真实发力。 */
test('自然wave在MIC13真实晚到526.8ms窗内越过原幅度门并平滑收回', () => {
  const trace = replay(lateCandidateAt, segmentEndAt);
  const active = trace.filter((row) => row.active);
  assert.ok(Math.max(...active.map((row) => row.output.ParamArmL)) > 0.16);
  assertSmoothReturn(trace, segmentEndAt);
});

/** 首合法PCM预判应在短句内达到并越过自身峰，而不是只在撤权前缓慢爬升到数值门。 */
test('首合法PCM预判的wave在真实句窗及相同526.8ms预算内完成主发力', () => {
  for (const cutoffAt of [segmentEndAt, firstLegalPcmRenderAt + originalLateWindowMs]) {
    const trace = replay(firstLegalPcmRenderAt, cutoffAt);
    const active = trace.filter((row) => row.active);
    const peak = active.reduce((best, row) =>
      row.output.ParamArmL > best.output.ParamArmL ? row : best
    );
    assert.ok(peak.output.ParamArmL > 0.16);
    assert.ok(peak.at < active.at(-1).at, '主发力峰须出现在当前句许可内');
    assert.ok(active.at(-1).output.ParamArmL < peak.output.ParamArmL);
    assertSmoothReturn(trace, cutoffAt);
  }
});

const originalProfiles = {
  command: [
    [8, 0.04168172687078868],
    [15, 0.0988993796251665],
    [30, 0.276104111731384],
    [42, 0.6604524628688508],
    [60, 0.4824909825790238],
    [90, 0.0682411534190206],
    [109, 0.007666846782778063],
    [110, 0.005862152042970738],
    [120, 0.00023977481985985107],
  ],
  listening: [
    [8, 0.012504518061236605],
    [15, 0.029669813887549953],
    [30, 0.08283123351941521],
    [42, 0.19813573886065525],
    [60, 0.14474729477370712],
    [90, 0.02047234602570618],
    [109, 0.002300054034833419],
    [110, 0.0017586456128912218],
    [120, 0.00007193244595795533],
  ],
};

/** 真实反馈后的幅度校准只缩放原轨迹；仍逐点核旧时间包络、指令1.65s、倾听比例及完成时序。 */
test('command与listening的wave清晰度校准保留原完整时间包络、完成时序及静音口型', () => {
  const calibratedAmplitudeRatio = 1.1461513790891597;
  for (const source of ['command', 'listening']) {
    const runtime = new AvatarPresentation();
    const phase = source === 'command' ? 'thinking' : 'listening';
    const expected = new Map(originalProfiles[source]);
    const acks = [];
    for (let frame = 0; frame <= 160; frame++) {
      const time = frame / 60;
      const output = runtime.advance({
        time,
        delta: frame ? 1 / 60 : 0,
        behavior: phase,
        delivery: {
          ...delivery('wave', phase, source === 'command' ? 'command' : 'dialogue'),
          style: 'neutral',
        },
        gaze: { x: 0, y: 0 },
        blink: 1,
        lip: null,
      });
      if (expected.has(frame))
        assert.ok(
          Math.abs(output.ParamArmL - expected.get(frame) * calibratedAmplitudeRatio) < 1e-12
        );
      assert.equal(output.ParamMouthOpenY, 0);
      acks.push(...runtime.drainAcknowledgments().map((ack) => ({ status: ack.status, at: time })));
    }
    assert.deepEqual(acks, [
      { status: 'started', at: 8 / 60 },
      { status: 'completed', at: 110 / 60 },
    ]);
  }
});

/** 固定物理时钟隔离时间包络，Jev强度与真实PCM分别传入，避免以提高声能掩盖低强度动作。 */
function uniformProfile(fps, energy, source, cancelAt = Infinity) {
  const runtime = new AvatarPresentation();
  const baseline = new AvatarPresentation();
  const trace = [];
  for (let frame = 0; frame <= fps * 3; frame++) {
    const time = frame / fps;
    const active = time < cancelAt;
    const cue = {
      ...delivery('wave', source === 'command' ? 'thinking' : 'speaking', source),
      energy,
      audio: { at: time, energy: 0.5, onset: frame === 8 ? 0.5 : 0 },
    };
    const input = {
      time,
      delta: frame ? 1 / fps : 0,
      behavior: active ? cue.phase : 'idle',
      delivery: active ? cue : null,
      gaze: { x: 0, y: 0 },
      blink: 1,
      lip: active ? { open: 0.5, form: -0.1 } : null,
    };
    const output = runtime.advance(input);
    const reference = baseline.advance({
      ...input,
      delivery: active ? { ...cue, gesture: 'none', gestureSource: 'none', gestureId: '' } : null,
    });
    assert.equal(output.ParamMouthOpenY, active ? 0.5 : 0);
    assert.equal(output.ParamMouthForm, active ? -0.1 : 0);
    assert.ok(Object.values(output).every(Number.isFinite));
    trace.push({ time, output, reference });
  }
  return trace;
}

/** 更早攻击仍须保留原45%幅度断言；多阶段撤权同时检查头身连续及600ms归位，不能只看抬臂峰。 */
test('自然wave在30/60fps与强度边界保持原比例，准备、峰值和回落取消不使头身尖跳', () => {
  for (const fps of [30, 60]) {
    for (const energy of [0, 0.45, 0.51, 1]) {
      const natural = uniformProfile(fps, energy, 'dialogue');
      const command = uniformProfile(fps, energy, 'command');
      const naturalPeak = Math.max(...natural.map((row) => row.output.ParamArmL));
      const commandPeak = Math.max(...command.map((row) => row.output.ParamArmL));
      assert.ok(Math.abs(naturalPeak / commandPeak - 0.45) < 0.02);
      assert.ok(naturalPeak < (0.55 + energy * 0.45) * 0.45);
      for (const cancelAt of [0.15, 0.35, 0.62, 1.1]) {
        const trace = uniformProfile(fps, energy, 'dialogue', cancelAt);
        for (let frame = 1; frame < trace.length; frame++) {
          for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX']) {
            assert.ok(
              Math.abs(trace[frame].output[id] - trace[frame - 1].output[id]) <
                (fps === 60 ? 2 : 3),
              `${fps}fps/${energy}/${cancelAt}/${id}不能产生头身跳变`
            );
          }
        }
        const settled = trace.find((row) => row.time >= cancelAt + 0.6);
        assert.ok(Math.abs(settled.output.ParamArmL) < 0.00003);
        assert.ok(Math.abs(settled.output.ParamAngleZ - settled.reference.ParamAngleZ) < 0.02);
        assert.ok(
          Math.abs(settled.output.ParamBodyAngleX - settled.reference.ParamBodyAngleX) < 0.004
        );
      }
    }
  }
});
