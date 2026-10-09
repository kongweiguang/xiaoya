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
  id: string;
  replyKey: string;
  style: AvatarStyle;
  gesture: AvatarGesture;
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
const gestureDurations = { none: 0, nod: 1.05, tilt: 1.35, wave: 1.65, shy: 1.8, shake: 1.35 };

/** 非有限输入退到安全中性值；控制器独立于 SDK，使网络异常不能把 NaN 送入网格。 */
function clamp(value: number, min: number, max: number, fallback = 0): number {
  return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
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
  private readonly seenDeliveries = new Set<string>();
  private activeGesture: { name: AvatarGesture; started: number } | null = null;
  private gestureRun: {
    replyKey: string;
    name: AvatarGesture;
    completed: boolean;
    resetAfterCompletion: boolean;
  } | null = null;
  private currentDelivery: string | null = null;
  private clock = 0;
  private energy = 0;

  /** 只保留无 SDK 的小状态；音频与会话所有权由外部持有，舞台重建即可彻底清空表现。 */
  constructor(expressions: AvatarExpressions = {}) {
    this.expressions = expressions;
    for (const [id, [, , baseline]] of Object.entries(limits)) this.pose[id] = baseline;
  }

  /** 先归档自然完成，保证同帧到达的 none 可建立间隔；取消不能冒充动作已经播完。 */
  private finishGesture() {
    if (
      !this.activeGesture ||
      this.clock - this.activeGesture.started < gestureDurations[this.activeGesture.name]
    )
      return;
    this.activeGesture = null;
    if (this.gestureRun) {
      this.gestureRun.completed = true;
      if (this.gestureRun.resetAfterCompletion) this.gestureRun.name = 'none';
      this.gestureRun.resetAfterCompletion = false;
    }
  }

  /** 段 ID 防重发、回复身份防连续同款；none 仅在自然完成后开放，取消保留已消费栅栏。 */
  private accept(delivery: AvatarDelivery | null) {
    if (!delivery || !delivery.id || !delivery.replyKey) {
      this.currentDelivery = null;
      this.activeGesture = null;
      if (this.gestureRun) this.gestureRun.resetAfterCompletion = false;
      return;
    }
    // 迟到旧段必须在修改当前动作之前拒绝，不能借重复 ID 抢走新回复正在播放的动作。
    if (delivery.id === this.currentDelivery || this.seenDeliveries.has(delivery.id)) return;
    this.currentDelivery = delivery.id;
    this.seenDeliveries.add(delivery.id);
    if (this.seenDeliveries.size > 32)
      this.seenDeliveries.delete(this.seenDeliveries.values().next().value!);
    if (this.gestureRun?.replyKey !== delivery.replyKey) {
      this.activeGesture = null;
      this.gestureRun = {
        replyKey: delivery.replyKey,
        name: 'none',
        completed: true,
        resetAfterCompletion: false,
      };
    }
    if (delivery.gesture === 'none') {
      if (this.gestureRun.completed) this.gestureRun.name = 'none';
      else if (this.activeGesture) this.gestureRun.resetAfterCompletion = true;
      return;
    }
    this.gestureRun.resetAfterCompletion = false;
    if (
      delivery.gesture === this.gestureRun.name ||
      !Object.hasOwn(gestureDurations, delivery.gesture)
    )
      return;
    this.gestureRun.name = delivery.gesture;
    this.gestureRun.completed = false;
    this.activeGesture = { name: delivery.gesture, started: this.clock };
  }

  /** 主动作压低轻摆；害羞用已有低头/收臂、摇头用左右曲线，嘴由宿主在物理后独占。 */
  advance(input: AvatarPresentationInput): Record<string, number> {
    this.clock = Math.max(this.clock, clamp(input.time, 0, Number.MAX_SAFE_INTEGER, this.clock));
    const dt = clamp(input.delta, 0, 0.05);
    this.finishGesture();
    this.accept(input.delivery);
    const thinking = input.behavior === 'thinking';
    const listening = input.behavior === 'listening';
    const confused = input.behavior === 'confused';
    const base: Record<string, number> = {};
    for (const [id, [, , baseline]] of Object.entries(limits)) base[id] = baseline;
    base.ParamAngleX = thinking ? 6 : clamp(input.gaze.x, -1, 1) * 6;
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

    const selected: ExpressionName =
      input.delivery && styles.has(input.delivery.style)
        ? input.delivery.style
        : confused
          ? 'concerned'
          : thinking
            ? 'thinking'
            : listening
              ? 'listening'
              : 'neutral';
    const target = { ...base };
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
      }
    }
    let speechSwayWeight = 1;
    if (this.activeGesture) {
      const { name, started } = this.activeGesture;
      const phase = clamp((this.clock - started) / gestureDurations[name], 0, 1);
      const envelope = Math.sin(Math.PI * phase) ** 2;
      speechSwayWeight = 1 - envelope * 0.7;
      if (name === 'nod') target.ParamAngleY -= 18 * Math.sin(phase * Math.PI * 2) * envelope;
      if (name === 'tilt') target.ParamAngleZ += 15 * envelope;
      if (name === 'wave') {
        target.ParamArmL = envelope * (0.8 + 0.18 * Math.sin(phase * Math.PI * 6));
        target.ParamAngleZ -= envelope * 3;
      }
      if (name === 'shy') {
        // 正式模型两侧正参数均向外展，负参数才向身体收拢；不虚构腮红或触碰嘴部。
        target.ParamArmL = -0.55 * envelope;
        target.ParamArmR = -0.55 * envelope;
        target.ParamAngleY -= 14 * envelope;
        target.ParamAngleX -= 6 * envelope;
        target.ParamAngleZ -= 5 * envelope;
      }
      if (name === 'shake') {
        // 一次左右往返用同一零端点包络，不能新建循环或在取消后继续补演。
        target.ParamAngleX += 24 * Math.sin(phase * Math.PI * 2) * envelope;
      }
    }
    // 音频包络只影响次级摆动，不猜测情绪；实际静音即关闭目标，保留轻微阻尼收尾。
    const open = clamp(input.lip?.open ?? 0, 0, 1);
    this.energy += (open - this.energy) * (1 - Math.exp(-dt * 12));
    target.ParamAngleX += Math.sin(this.clock * 3.1) * this.energy * 1.8 * speechSwayWeight;
    target.ParamAngleY += Math.sin(this.clock * 4.2) * this.energy * 1.2 * speechSwayWeight;
    const output: Record<string, number> = {};
    for (const [id, [min, max]] of Object.entries(limits)) {
      this.pose[id] += (clamp(target[id], min, max) - this.pose[id]) * (1 - Math.exp(-dt * 12));
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
