import type { AvatarBehavior } from './behavior';

export type AvatarStyle =
  | 'neutral'
  | 'happy'
  | 'gentle'
  | 'concerned'
  | 'curious'
  | 'shy'
  | 'surprised';
export type AvatarGesture = 'none' | 'nod' | 'tilt' | 'wave' | 'shy' | 'shake';
export type AvatarDelivery = {
  instance: string;
  id: string;
  replyKey: string;
  style: AvatarStyle;
  gesture: AvatarGesture;
  phase: 'idle' | 'listening' | 'thinking' | 'speaking';
  turnId: string;
  segmentKey: string;
  gestureId: string;
  gestureSource: 'none' | 'dialogue' | 'command';
  energy: number;
  audio?: { at: number; energy: number; onset: number };
};
export type PresentationAck = {
  instance: string;
  turn_id: string;
  gesture_id: string;
  status: 'started' | 'completed' | 'canceled' | 'unavailable';
  stroke_started?: boolean;
};
type ExpressionName = AvatarStyle | 'listening' | 'thinking';
type Blend = 'Add' | 'Multiply' | 'Overwrite';
export type AvatarExpression = {
  fadeIn: number;
  fadeOut: number;
  parameters: { id: string; value: number; blend: Blend }[];
};
export type AvatarExpressions = Partial<Record<ExpressionName, AvatarExpression>>;
type AvatarPresentationInput = {
  time: number;
  delta: number;
  behavior: AvatarBehavior;
  delivery: AvatarDelivery | null;
  gaze: { x: number; y: number };
  blink: number;
  lip: { open: number; form: number } | null;
};

// 每帧从同一基准合成，资源中的 Add 只表示相对基准而不是累加上一帧。
const limits: Record<string, readonly [number, number, number]> = {
  ParamAngleX: [-30, 30, 0],
  ParamAngleY: [-30, 30, 0],
  ParamAngleZ: [-30, 30, 0],
  ParamBodyAngleX: [-30, 30, 0],
  ParamEyeBallX: [-1, 1, 0],
  ParamEyeBallY: [-1, 1, 0],
  ParamEyeLOpen: [0, 1, 1],
  ParamEyeROpen: [0, 1, 1],
  ParamEyeSmile: [0, 1, 0],
  ParamBrowLY: [-1, 1, 0],
  ParamBrowRY: [-1, 1, 0],
  ParamBrowLAngle: [-1, 1, 0],
  ParamBrowRAngle: [-1, 1, 0],
  ParamBreath: [0, 1, 0.5],
  ParamLeafSwing: [-1, 1, 0],
  ParamArmL: [-1, 1, 0],
  ParamArmR: [-1, 1, 0],
};
const expressionNames: readonly ExpressionName[] = [
  'neutral',
  'happy',
  'gentle',
  'concerned',
  'curious',
  'shy',
  'surprised',
  'listening',
  'thinking',
];
const styles = new Set(['neutral', 'happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised']);
const gestureDurations = { none: 0, nod: 1.15, tilt: 1.35, wave: 1.65, shy: 1.8, shake: 1.35 };

/** 非有限输入退到安全中性值；控制器独立于 SDK，使网络异常不能把 NaN 送入网格。 */
function clamp(value: number, min: number, max: number, fallback = 0): number {
  return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}

/** 两端导数为零，让准备和收回接入主动作时不产生速度尖峰。 */
function smooth(amount: number): number {
  return amount * amount * (3 - 2 * amount);
}

/** 短句挥手提前平滑到峰并短暂停留，让原阻尼来得及发力；目标不超过原幅度，撤权仍可随时收回。 */
function speakingWaveShape(phase: number): number {
  return (
    (0.12 + 0.88 * smooth(clamp(phase / 0.2, 0, 1))) *
    (1 - smooth(clamp((phase - 0.36) / 0.64, 0, 1)))
  );
}

/** 加大可见幅度后稍提前翻向并延长翻向肩部，分散速度峰；总时长与原短句内俯仰机会不变。 */
function speakingNodShape(phase: number): number {
  const peak = 0.62;
  const time = phase * gestureDurations.nod;
  const lowerEnd = 0.06;
  const liftStart = lowerEnd + 0.06;
  const liftEnd = liftStart + 0.24;
  const settleStart = liftEnd + 0.1;
  if (time < lowerEnd) return peak * smooth(clamp(time / lowerEnd, 0, 1));
  if (time < liftStart) return peak;
  if (time < liftEnd) return peak * (1 - 2 * speakingNodTurn((time - liftStart) / 0.24));
  if (time < settleStart) return -peak;
  return (
    -peak * (1 - smooth(clamp((time - settleStart) / (gestureDurations.nod - settleStart), 0, 1)))
  );
}

/** 普通强度增加辨识度，高强度连续收敛原上限且保留严格能量层次；各来源共用增益，不抬高倾听占比。 */
function gestureClarity(gesture: AvatarGesture, strength: number): number {
  if (gesture === 'shy') return 1.2;
  const gain = gesture === 'wave' ? 0.15 : 0.2;
  if (strength <= 0.5) return 1 + gain;
  const original = 0.55 + strength * 0.45;
  const middle = (0.55 + 0.5 * 0.45) * (1 + gain);
  return (middle + (1 - middle) * ((strength - 0.5) / 0.5)) / original;
}

/** 过中位时分配更多时间，幅度增大后仍是一次柔和左右往返；两端位置不变，不延长整段或倾听/指令节奏。 */
function speakingShakePhase(phase: number): number {
  return phase + (0.2 * Math.sin(phase * Math.PI * 2)) / (Math.PI * 2);
}

/** 翻向两端连接零速度、零加速度，中段匀速分散速度峰；只重分配位移，不增加执行状态或绕过取消。 */
function speakingNodTurn(amount: number): number {
  const shoulderRatio = 0.1;
  /** 四次积分肩部与匀速段连续衔接，避免短点头在翻向时突然改变速度。 */
  const shoulder = (u: number): number =>
    ((u * u * u - (u * u * u * u) / 2) * shoulderRatio) / (1 - shoulderRatio);
  return amount < shoulderRatio
    ? shoulder(amount / shoulderRatio)
    : amount > 1 - shoulderRatio
      ? 1 - shoulder((1 - amount) / shoulderRatio)
      : (amount - shoulderRatio / 2) / (1 - shoulderRatio);
}

/** 只加载本项目明确支持的表情，名字不直接变成文件路径或任意参数执行入口。 */
export function isAvatarExpressionName(name: string): name is ExpressionName {
  return expressionNames.includes(name as ExpressionName);
}

/** 表情不能拥有嘴部、物理或肢体；非法配置显式失败，避免悄悄覆盖音频与动作通道。 */
export function parseAvatarExpression(value: unknown): AvatarExpression {
  if (!value || typeof value !== 'object') throw new Error('人物表情配置无效');
  const source = value as Record<string, unknown>;
  if (source.Type !== 'Live2D Expression' || !Array.isArray(source.Parameters))
    throw new Error('人物表情配置无效');
  const parameters: AvatarExpression['parameters'] = [];
  const seen = new Set<string>();
  for (const parameter of source.Parameters) {
    if (!parameter || typeof parameter !== 'object') throw new Error('人物表情参数无效');
    const { Id: id, Value: amount, Blend: blend } = parameter;
    if (
      typeof id !== 'string' ||
      !(id in limits) ||
      !/^Param(Eye|Brow|Angle)/.test(id) ||
      typeof amount !== 'number' ||
      !Number.isFinite(amount) ||
      !['Add', 'Multiply', 'Overwrite'].includes(blend) ||
      seen.has(id)
    )
      throw new Error('人物表情参数无效');
    const [min, max] = limits[id];
    if (amount < min || amount > max) throw new Error('人物表情参数越界');
    parameters.push({ id, value: amount, blend });
    seen.add(id);
  }
  return {
    fadeIn: clamp(Number(source.FadeInTime ?? 0.3), 0.05, 2, 0.3),
    fadeOut: clamp(Number(source.FadeOutTime ?? 0.35), 0.05, 2, 0.35),
    parameters,
  };
}

export class AvatarPresentation {
  private readonly expressions: AvatarExpressions;
  private readonly weights = new Map<ExpressionName, number>();
  private readonly pose: Record<string, number> = {};
  private readonly velocity: Record<string, number> = {};
  private readonly seenDeliveries = new Set<string>();
  private readonly seenGestures = new Set<string>();
  private activeGesture: {
    delivery: AvatarDelivery;
    name: AvatarGesture;
    prepared: number;
    stroke: number | null;
    strength: number;
    duration: number;
    audioAt: number;
  } | null = null;
  private readonly acknowledgments: PresentationAck[] = [];
  private readonly naturalStarts: number[] = [];
  private readonly lastNatural = new Map<AvatarGesture, number>();
  private pendingNatural: AvatarDelivery | null = null;
  private currentTurn: string | null = null;
  private currentDelivery: string | null = null;
  private clock = 0;
  private energy = 0;
  private selectedExpression: ExpressionName = 'neutral';
  private expressionUntil = 0;
  private speechAccent = { x: 0, y: 0 };
  private lastAudioAt = -Infinity;
  private accentDirection = 1;
  private gestureOwnership = { x: 0, y: 0, z: 0 };
  private gestureOffset = { x: 0, y: 0, z: 0 };
  private releasedOwnership: {
    at: number;
    x: number;
    y: number;
    z: number;
    offset: { x: number; y: number; z: number };
  } | null = null;

  /** 只保留无 SDK 的小状态；音频与会话所有权由外部持有，舞台重建即可彻底清空表现。 */
  constructor(expressions: AvatarExpressions = {}) {
    this.expressions = expressions;
    for (const [id, [, , baseline]] of Object.entries(limits)) {
      this.pose[id] = baseline;
      this.velocity[id] = 0;
    }
  }

  /** 完成只由实际stroke归档并明确其事实，结果等模型绘制成功后才由Runtime转发。 */
  private finishGesture() {
    if (
      !this.activeGesture ||
      this.activeGesture.stroke === null ||
      this.clock - this.activeGesture.stroke < this.activeGesture.duration
    )
      return;
    this.acknowledge(this.activeGesture.delivery, 'completed', true);
    this.activeGesture = null;
  }

  /** 冷却只延后当前自然候选，不吞掉整句；等待槽随精确身份撤销，指令仍直接走原准备与幅度。 */
  private accept(delivery: AvatarDelivery | null) {
    if (
      !delivery ||
      !delivery.id ||
      !delivery.instance ||
      !delivery.turnId ||
      !delivery.replyKey ||
      !['idle', 'listening', 'thinking', 'speaking'].includes(delivery.phase)
    ) {
      this.currentDelivery = null;
      this.cancel();
      return;
    }
    const turn = JSON.stringify([delivery.instance, delivery.turnId]);
    if (turn !== this.currentTurn) {
      this.cancel();
      this.currentTurn = turn;
      this.selectedExpression = 'neutral';
      this.expressionUntil = 0;
      this.lastAudioAt = -Infinity;
      this.speechAccent = { x: 0, y: 0 };
    }
    // 自然动作属于当前倾听阶段或当前音频片段；新句不能借上一句或倾听动作继续发力。
    if (
      this.activeGesture?.delivery.gestureSource === 'dialogue' &&
      (this.activeGesture.delivery.phase !== delivery.phase ||
        (delivery.phase === 'speaking' &&
          this.activeGesture.delivery.segmentKey !== delivery.segmentKey))
    )
      this.cancel();
    if (
      this.pendingNatural &&
      (delivery.gestureSource !== 'dialogue' ||
        this.pendingNatural.instance !== delivery.instance ||
        this.pendingNatural.turnId !== delivery.turnId ||
        this.pendingNatural.replyKey !== delivery.replyKey ||
        this.pendingNatural.segmentKey !== delivery.segmentKey ||
        this.pendingNatural.phase !== delivery.phase ||
        this.pendingNatural.gestureId !== delivery.gestureId ||
        this.pendingNatural.gesture !== delivery.gesture)
    )
      this.pendingNatural = null;
    // 迟到旧段必须在修改当前动作之前拒绝，不能借重复 ID 抢走新回复正在播放的动作。
    if (delivery.id === this.currentDelivery || this.seenDeliveries.has(delivery.id)) return;
    this.currentDelivery = delivery.id;
    this.seenDeliveries.add(delivery.id);
    if (this.seenDeliveries.size > 32)
      this.seenDeliveries.delete(this.seenDeliveries.values().next().value!);
    if (delivery.gesture === 'none' || !delivery.gestureId) {
      // 当前倾听的 none 撤去 VAD/ASR 许可；说话同片段的表情更新则保留已开始动作。
      const listeningRevoked =
        delivery.phase === 'listening' &&
        this.activeGesture?.delivery.phase === 'listening' &&
        this.activeGesture.delivery.gestureSource === 'dialogue' &&
        this.activeGesture.delivery.instance === delivery.instance &&
        this.activeGesture.delivery.turnId === delivery.turnId;
      if (listeningRevoked || this.activeGesture?.delivery.gestureSource === 'command')
        this.cancel();
      return;
    }
    if (
      delivery.gestureSource !== 'command' &&
      (delivery.gestureSource !== 'dialogue' || !['listening', 'speaking'].includes(delivery.phase))
    )
      return;
    const key = JSON.stringify([delivery.instance, delivery.turnId, delivery.gestureId]);
    if (this.seenGestures.has(key) || !Object.hasOwn(gestureDurations, delivery.gesture)) return;
    this.seenGestures.add(key);
    if (this.seenGestures.size > 128)
      this.seenGestures.delete(this.seenGestures.values().next().value!);
    const command = delivery.gestureSource === 'command';
    if (!command && this.naturalBlocked(delivery.gesture)) {
      this.pendingNatural = delivery;
      return;
    }
    this.prepareGesture(delivery);
  }

  /** 倾听与说话共享原频率预算；只在真实发力时记账，等待不会重置或绕开任何间隔。 */
  private naturalBlocked(gesture: AvatarGesture): boolean {
    while (this.naturalStarts.length && this.clock - this.naturalStarts[0] >= 10)
      this.naturalStarts.shift();
    return Boolean(
      this.activeGesture ||
        this.clock - (this.naturalStarts.at(-1) ?? -Infinity) < 3 ||
        this.clock - (this.lastNatural.get(gesture) ?? -Infinity) < 6 ||
        this.naturalStarts.length >= 2
    );
  }

  /** accept已核对当前身份；每帧只重评一个等待槽，说话必须仍有真实PCM，不能借口型或下一句补演。 */
  private resumeNatural(delivery: AvatarDelivery | null): void {
    if (
      !this.pendingNatural ||
      !delivery ||
      this.naturalBlocked(this.pendingNatural.gesture) ||
      (this.pendingNatural.phase === 'speaking' &&
        !(delivery.audio && delivery.audio.energy > 0.025))
    )
      return;
    this.prepareGesture({ ...this.pendingNatural, audio: delivery.audio });
  }

  /** 同一个准备入口保证冷却后采纳仍使用原准备期、能量、曲线和阻尼，命令不受等待槽限制。 */
  private prepareGesture(delivery: AvatarDelivery): void {
    this.cancel();
    const strength = clamp(delivery.energy, 0, 1, 0.5);
    this.activeGesture = {
      delivery,
      name: delivery.gesture,
      prepared: this.clock,
      stroke: null,
      strength,
      // 说话自然挥手须在短PCM句内清楚发力；仅缩时间，不放大幅度或借下一句，倾听仍保持轻缓节奏。
      duration:
        (delivery.gesture === 'wave' &&
        delivery.gestureSource === 'dialogue' &&
        delivery.phase === 'speaking'
          ? 0.9
          : gestureDurations[delivery.gesture]) *
        (1.1 - strength * 0.15),
      audioAt: delivery.audio?.at ?? -Infinity,
    };
  }

  /** 渲染器先绘制模型再取回事件，网络侧不会把创建候选误当作动作已执行。 */
  drainAcknowledgments(): PresentationAck[] {
    return this.acknowledgments.splice(0);
  }

  /** 撤权当帧结束执行许可，仅把上一帧主轴目标在原交接窗内归零，较大姿态不会突然抽回或重发回执。 */
  cancel(): void {
    this.pendingNatural = null;
    if (this.activeGesture) {
      this.acknowledge(this.activeGesture.delivery, 'canceled', this.activeGesture.stroke !== null);
      this.releasedOwnership = {
        at: this.clock,
        ...this.gestureOwnership,
        offset: { ...this.gestureOffset },
      };
    }
    this.activeGesture = null;
  }

  /** 回执只报告本动画的stroke事实，不能以队列中缺started推断未执行；服务器仍独立授权。 */
  private acknowledge(
    delivery: AvatarDelivery,
    status: PresentationAck['status'],
    strokeStarted: boolean
  ): void {
    if (delivery.gestureSource === 'command' || delivery.gestureSource === 'dialogue')
      this.acknowledgments.push({
        instance: delivery.instance,
        turn_id: delivery.turnId,
        gesture_id: delivery.gestureId,
        status,
        stroke_started: strokeStarted,
      });
  }

  /** 表情有最短停留，紧急惊讶和结束可立即切换；同类更新延续神态而不重启淡化。 */
  private expression(requested: ExpressionName, active: boolean): ExpressionName {
    if (requested === this.selectedExpression) return requested;
    if (!active || requested === 'surprised' || this.clock >= this.expressionUntil) {
      this.selectedExpression = requested;
      this.expressionUntil = this.clock + 1.1;
    }
    return this.selectedExpression;
  }

  /** 同源增益只校准模型动作目标，许可、时长与真实stroke回执不变；撤权以原短交接窗平滑归还主轴。 */
  advance(input: AvatarPresentationInput): Record<string, number> {
    this.clock = Math.max(this.clock, clamp(input.time, 0, Number.MAX_SAFE_INTEGER, this.clock));
    const dt = clamp(input.delta, 0, 0.05);
    this.finishGesture();
    this.accept(input.delivery);
    this.resumeNatural(input.delivery);
    const thinking = input.delivery?.phase === 'thinking' || input.behavior === 'thinking';
    const listening = input.delivery?.phase === 'listening' || input.behavior === 'listening';
    const confused = input.behavior === 'confused';
    const base: Record<string, number> = {};
    for (const [id, [, , baseline]] of Object.entries(limits)) base[id] = baseline;
    base.ParamAngleX = thinking ? 2 : clamp(input.gaze.x, -1, 1) * 2;
    base.ParamAngleY = listening ? 1.5 : thinking ? 3 : 0;
    base.ParamAngleZ = confused
      ? -7
      : thinking
        ? 4
        : listening
          ? -2
          : Math.sin(this.clock * 0.63) * 1.2;
    base.ParamEyeBallX = thinking ? 0.4 : listening ? 0 : clamp(input.gaze.x, -1, 1);
    base.ParamEyeBallY = thinking ? 0.2 : listening ? 0 : clamp(input.gaze.y, -1, 1);
    base.ParamBreath = (Math.sin(this.clock * 1.8) + 1) / 2;
    base.ParamLeafSwing = Math.sin(this.clock * 1.25) * 0.18;

    // 眉眼语义只来自受控 Jev 状态；生命周期保留姿态反馈，但不自行猜测情绪。
    const requested: ExpressionName =
      input.delivery && styles.has(input.delivery.style) ? input.delivery.style : 'neutral';
    const continuous = !input.delivery && ['speaking', 'thinking'].includes(input.behavior);
    const selected = this.expression(
      continuous ? this.selectedExpression : requested,
      Boolean(input.delivery) || continuous
    );
    const target = { ...base };
    const background = { ...base };
    // 各资源都对相同基准求差再交叉淡化，Multiply 与 Overwrite 不会因资源加载顺序改变结果。
    for (const name of expressionNames) {
      const expression = this.expressions[name];
      if (!expression) continue;
      const previous = this.weights.get(name) ?? 0;
      const goal = selected === name ? 1 : 0;
      const fade = goal ? expression.fadeIn : expression.fadeOut;
      const weight = previous + (goal - previous) * (1 - Math.exp((-dt * 4) / fade));
      this.weights.set(name, weight);
      for (const parameter of expression.parameters) {
        const original = base[parameter.id];
        const value =
          parameter.blend === 'Add'
            ? original + parameter.value
            : parameter.blend === 'Multiply'
              ? original * parameter.value
              : parameter.value;
        target[parameter.id] += (value - original) * weight;
        // 只追踪资源混合后仍属于背景的部分；Overwrite 的语义目标不能随待机一起扣掉。
        if (parameter.blend !== 'Add')
          background[parameter.id] +=
            (parameter.blend === 'Multiply' ? original * (parameter.value - 1) : -original) *
            weight;
      }
    }
    let speechSwayWeight = 1;
    const ownership = { x: 0, y: 0, z: 0 };
    const offset = { x: 0, y: 0, z: 0 };
    if (this.activeGesture) {
      const action = this.activeGesture;
      const prepared = this.clock - action.prepared;
      const audibleEnergy = input.delivery?.audio?.energy ?? input.lip?.open ?? 0;
      const onset = input.delivery?.audio?.onset ?? 0;
      // 准备之后等待一个真实发声窗口；没有声音的过期动作直接作废，不用字幕猜重音。
      const command = action.delivery.gestureSource === 'command';
      const listeningAction = action.delivery.phase === 'listening';
      if (command && action.stroke === null && prepared > 1.5) {
        this.acknowledge(action.delivery, 'unavailable', false);
        this.activeGesture = null;
      }
      if (!command && !listeningAction && action.stroke === null && prepared > 0.8) this.cancel();
      if (
        this.activeGesture &&
        action.stroke === null &&
        prepared >= 0.12 &&
        (command ||
          listeningAction ||
          (audibleEnergy > 0.025 &&
            ((onset > 0.01 && (input.delivery?.audio?.at ?? -Infinity) > action.audioAt) ||
              prepared >= 0.32 ||
              !input.delivery?.audio)))
      ) {
        action.stroke = this.clock;
        this.acknowledge(action.delivery, 'started', true);
        if (!command) {
          this.naturalStarts.push(this.clock);
          this.lastNatural.set(action.name, this.clock);
        }
      }
      const { name, strength, duration } = action;
      const strokePhase =
        action.stroke === null ? 0 : clamp((this.clock - action.stroke) / duration, 0, 1);
      const phase = strokePhase < 0.72 ? strokePhase / 0.72 : 1;
      const motionPhase =
        name === 'shake' && !command && !listeningAction ? speakingShakePhase(phase) : phase;
      const preparation = clamp(prepared / 0.12, 0, 1);
      const retraction = clamp((strokePhase - 0.72) / 0.28, 0, 1);
      const shape = !this.activeGesture
        ? 0
        : action.stroke === null
          ? 0.12 * smooth(preparation)
          : name === 'wave' && !command && !listeningAction
            ? speakingWaveShape(strokePhase)
            : strokePhase < 0.72
              ? 0.12 + 0.88 * Math.sin(Math.PI * motionPhase) ** 2
              : 0.12 * (1 - smooth(retraction));
      const originalEnvelope =
        shape * (0.55 + strength * 0.45) * (command ? 1 : listeningAction ? 0.3 : 0.45);
      const clarity = gestureClarity(name, strength);
      const envelope = originalEnvelope * clarity;
      // 准备时让出主轴、收回时交还，接管权独立于能量，低强度语义才不会被反向待机吞掉。
      const mainOwnership = !this.activeGesture
        ? 0
        : action.stroke === null
          ? smooth(preparation)
          : 1 - smooth(retraction);
      if (name === 'shake' || name === 'shy') {
        ownership.x = mainOwnership;
      }
      if (name === 'nod' || name === 'shy') {
        ownership.y = mainOwnership;
      }
      if (name === 'tilt' || name === 'wave' || name === 'shy') ownership.z = mainOwnership;
      speechSwayWeight = 1 - originalEnvelope * 0.7;
      if (name === 'nod')
        offset.y -=
          42 *
          (!command && !listeningAction && action.stroke !== null
            ? speakingNodShape(strokePhase) * (0.55 + strength * 0.45) * 0.45 * clarity
            : Math.sin(phase * Math.PI * 2) * envelope);
      if (name === 'tilt') offset.z += 30 * envelope;
      if (name === 'wave') {
        target.ParamArmL = envelope * (0.8 + 0.18 * Math.sin(phase * Math.PI * 6));
        offset.z -= envelope * 3;
      }
      if (name === 'shy') {
        // 正式模型两侧正参数均向外展，负参数才向身体收拢；不虚构腮红或触碰嘴部。
        target.ParamArmL = -0.55 * envelope;
        target.ParamArmR = -0.55 * envelope;
        offset.y -= 14 * envelope;
        offset.x -= 6 * envelope;
        offset.z -= 5 * envelope;
      }
      if (name === 'shake') {
        // 一次左右往返用同一零端点包络，不能新建循环或在取消后继续补演。
        offset.x += 44 * Math.sin(motionPhase * Math.PI * 2) * envelope;
      }
    }
    // 取消只留下短暂的背景交接，不保留动作、口型或执行许可；新动作可同时接管自己的主轴。
    const release = this.releasedOwnership
      ? 1 - smooth(clamp((this.clock - this.releasedOwnership.at) / 0.12, 0, 1))
      : 0;
    for (const axis of ['x', 'y', 'z'] as const)
      this.gestureOwnership[axis] = Math.max(
        ownership[axis],
        (this.releasedOwnership?.[axis] ?? 0) * release
      );
    // 仅为姿态交接保留有限衰减目标；新动作按自己的接管权替换旧目标，不叠加两个主动作。
    for (const [axis, parameter] of [
      ['x', 'ParamAngleX'],
      ['y', 'ParamAngleY'],
      ['z', 'ParamAngleZ'],
    ] as const)
      target[parameter] +=
        offset[axis] +
        (this.releasedOwnership?.offset[axis] ?? 0) * release * (1 - ownership[axis]);
    this.gestureOffset = offset;
    if (!release) this.releasedOwnership = null;
    target.ParamAngleX -= background.ParamAngleX * this.gestureOwnership.x;
    target.ParamAngleY -= background.ParamAngleY * this.gestureOwnership.y;
    target.ParamAngleZ -= background.ParamAngleZ * this.gestureOwnership.z;
    // 音频包络只影响次级摆动，不猜测情绪；实际静音即关闭目标，保留轻微阻尼收尾。
    const open = clamp(input.lip?.open ?? 0, 0, 1);
    const audioEnergy = clamp(input.delivery?.audio?.energy ?? open, 0, 1);
    this.energy += (audioEnergy - this.energy) * (1 - Math.exp(-dt * 12));
    const audioAt = input.delivery?.audio?.at ?? -Infinity;
    const onset = clamp(input.delivery?.audio?.onset ?? 0, 0, 1);
    if (audioAt > this.lastAudioAt) {
      this.lastAudioAt = audioAt;
      if (onset > 0.01 && audioEnergy > 0.025) {
        this.accentDirection *= -1;
        this.speechAccent.x = this.accentDirection * (0.6 + audioEnergy * 1.1);
        this.speechAccent.y = -0.4 - audioEnergy * 0.9;
      }
    }
    this.speechAccent.x *= Math.exp(-dt * 4.5);
    this.speechAccent.y *= Math.exp(-dt * 5.5);
    target.ParamAngleX += this.speechAccent.x * speechSwayWeight * (1 - this.gestureOwnership.x);
    target.ParamAngleY +=
      (this.speechAccent.y + this.energy * 0.35) * speechSwayWeight * (1 - this.gestureOwnership.y);
    // 小幅身摆跟随最终头姿态，复用现有速度与回收；不另建时钟、语义判断或动作循环。
    target.ParamBodyAngleX = target.ParamAngleX * 0.18 + target.ParamAngleZ * 0.12;
    const output: Record<string, number> = {};
    for (const [id, [min, max]] of Object.entries(limits)) {
      const goal = clamp(target[id], min, max);
      const displacement = this.pose[id] - goal;
      const damping = 24;
      const decay = Math.exp(-damping * dt);
      const momentum = this.velocity[id] + damping * displacement;
      this.pose[id] = clamp(goal + (displacement + momentum * dt) * decay, min, max);
      this.velocity[id] = (this.velocity[id] - damping * momentum * dt) * decay;
      if (this.pose[id] === min || this.pose[id] === max) this.velocity[id] = 0;
      output[id] = this.pose[id];
    }
    // 眨眼是面部最后一个遮罩，不能被快乐资源的眼睛开度重新撑开。
    output.ParamEyeLOpen *= clamp(input.blink, 0, 1, 1);
    output.ParamEyeROpen *= clamp(input.blink, 0, 1, 1);
    output.ParamMouthOpenY = open;
    output.ParamMouthForm = clamp(input.lip?.form ?? 0, -1, 1);
    return output;
  }
}
