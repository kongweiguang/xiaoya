import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const { checkPackage } = require('./package-checks.cjs');
const repository = path.resolve(import.meta.dirname, '../../../..');
// 当前包的显式约定与浏览器后备图一致；正式换包时一起更新，不从 MOC 目录猜测图片。
const currentPoster = 'concept-v1/poster.png';
const ranges = {
  ParamAngleX: [-30, 30, 0], ParamAngleY: [-30, 30, 0], ParamAngleZ: [-30, 30, 0],
  ParamBodyAngleX: [-30, 30, 0], ParamEyeLOpen: [0, 1, 1], ParamEyeROpen: [0, 1, 1],
  ParamEyeBallX: [-1, 1, 0], ParamEyeBallY: [-1, 1, 0], ParamEyeSmile: [0, 1, 0],
  ParamBrowLY: [-1, 1, 0], ParamBrowRY: [-1, 1, 0], ParamBrowLAngle: [-1, 1, 0],
  ParamBrowRAngle: [-1, 1, 0], ParamMouthOpenY: [0, 1, 0], ParamMouthForm: [-1, 1, 0],
  ParamLeafSwing: [-1, 1, 0], ParamArmL: [-1, 1, 0], ParamArmR: [-1, 1, 0], ParamBreath: [0, 1, 0],
};
const styles = ['neutral', 'happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised'];
const gestures = ['nod', 'tilt', 'wave', 'shy', 'shake'];

/** 不接受包外链接；候选图集可以重排，但引用必须随资产包一起交付。 */
function asset(directory, relative) {
  const root = fs.realpathSync(directory);
  const target = fs.realpathSync(path.resolve(root, relative));
  if (!target.startsWith(root + path.sep)) throw new Error('模型资源超出资产包');
  return fs.readFileSync(target);
}

/** 使用官方公开版本查询确认 WASM 已就绪，不把固定延时当成可用证明。 */
async function officialCore() {
  const file = path.join(repository, 'web/public/avatar/vendor/live2dcubismcore.min.js');
  const context = { require, __dirname: path.dirname(file), __filename: file, process, Buffer,
    console, WebAssembly, setTimeout, clearTimeout, TextDecoder, TextEncoder, ArrayBuffer,
    Uint8Array, Float32Array, Int32Array, Uint32Array, Int16Array, Uint16Array };
  context.global = context;
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), context, { filename: file });
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    try {
      if (context.Live2DCubismCore.Version.csmGetVersion() > 0) return context.Live2DCubismCore;
    } catch { /* WASM 实例仍在加载；只在这一个就绪门重试。 */ }
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw new Error('官方 Core 初始化超时');
}

/** 任意网格布局都可比较真实顶点；同时允许闭眼退化，不要求旧 drawable 名称。 */
function geometry(model) {
  return model.drawables.vertexPositions.map(vertices => Array.from(vertices));
}

/** 每项姿态从默认值开始，避免上一帧残留让无绑定参数得到假阳性。 */
function pose(model, values = {}) {
  model.parameters.values.set(model.parameters.defaultValues);
  for (const [id, value] of Object.entries(values)) {
    const index = model.parameters.ids.indexOf(id);
    if (index < 0 || !Number.isFinite(value)) throw new Error('姿态参数不存在或非有限值');
    if (value < model.parameters.minimumValues[index] || value > model.parameters.maximumValues[index])
      throw new Error('姿态参数超出模型范围');
    model.parameters.values[index] = value;
  }
  model.update();
  const vertices = geometry(model);
  if (vertices.some(mesh => mesh.some(value => !Number.isFinite(value)))) throw new Error('网格出现非有限坐标');
  return vertices;
}

/** 对所有网格计算最大源像素位移，只证明绑定有效，外观必须另做截图审查。 */
function displacement(before, after, pixelsPerUnit) {
  let maximum = 0;
  for (let mesh = 0; mesh < before.length; mesh++)
    for (let vertex = 0; vertex < before[mesh].length; vertex++)
      maximum = Math.max(maximum, Math.abs(before[mesh][vertex] - after[mesh][vertex]) * pixelsPerUnit);
  return maximum;
}

/** 相对默认绕序检查翻面，不假定旧单网格或固定顶点数量，闭眼可降为线。 */
function winding(model, baseline, vertices) {
  for (let mesh = 0; mesh < vertices.length; mesh++) {
    const indices = model.drawables.indices[mesh];
    for (let triangle = 0; triangle < indices.length; triangle += 3) {
      const original = triangleArea(baseline[mesh], indices, triangle);
      const current = triangleArea(vertices[mesh], indices, triangle);
      if (current * Math.sign(original) < -1e-8) throw new Error('组合姿态导致网格翻面');
    }
  }
}

/** 实际三角形索引是唯一几何来源，图集字节及轮廓布局不参与约束。 */
function triangleArea(vertices, indices, offset) {
  const a = indices[offset] * 2, b = indices[offset + 1] * 2, c = indices[offset + 2] * 2;
  return (vertices[b] - vertices[a]) * (vertices[c + 1] - vertices[a + 1])
    - (vertices[c] - vertices[a]) * (vertices[b + 1] - vertices[a + 1]);
}

/** 当前海报显式进入资产闭包；候选可指定自己的图，但不得用历史根海报或目录猜测补位。 */
export async function validateModel(directory, { stable = false, poster = currentPoster } = {}) {
  const manifest = JSON.parse(asset(directory, 'xiaoya.model3.json'));
  const bytes = asset(directory, manifest.FileReferences.Moc);
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const core = await officialCore();
  const moc = core.Moc.fromArrayBuffer(buffer);
  if (!moc || moc.hasMocConsistency(buffer) !== 1) throw new Error('官方 Core 拒绝模型一致性');
  let model;
  try {
    model = core.Model.fromMoc(moc);
    if (!model || model.parameters.count !== 19 || new Set(model.parameters.ids).size !== 19)
      throw new Error('模型必须恰有 19 个独立参数');
    const blendShapeCount = Array.from(model.parameters.types).filter(type => type === core.ParameterType_BlendShape).length;
    if (stable && blendShapeCount) throw new Error('稳定版候选不得含 BlendShape');
    if (model.drawables.count > 100) throw new Error('网格数量超出 FREE 限制');
    const parameterChecks = {};
    const baseline = pose(model);
    for (let index = 0; index < model.parameters.count; index++) {
      const id = model.parameters.ids[index];
      const limits = [model.parameters.minimumValues[index], model.parameters.maximumValues[index], model.parameters.defaultValues[index]];
      if (!ranges[id] || limits.some((value, part) => value !== ranges[id][part])) throw new Error('参数契约不一致：' + id);
      const context = id === 'ParamMouthForm' ? { ParamMouthOpenY: 1 } : {};
      const neutral = pose(model, context);
      let maximum = 0;
      for (const value of limits.slice(0, 2)) {
        const vertices = pose(model, { ...context, [id]: value });
        winding(model, baseline, vertices);
        maximum = Math.max(maximum, displacement(neutral, vertices, model.canvasinfo.PixelsPerUnit));
      }
      if (maximum < 0.01) throw new Error('声明参数无有效绑定：' + id);
      parameterChecks[id] = { min: limits[0], max: limits[1], default: limits[2], displacementPixels: maximum };
    }
    const expressions = {};
    const { AvatarPresentation, parseAvatarExpression } = await import(pathToFileURL(path.join(repository, 'web/lib/avatar/presentation.ts')));
    for (const reference of manifest.FileReferences.Expressions)
      expressions[reference.Name] = parseAvatarExpression(JSON.parse(asset(directory, reference.File)));
    for (const style of styles.filter(name => name !== 'neutral'))
      if (!expressions[style]) throw new Error('缺少风格资源：' + style);
    let combinationFrames = 0;
    for (const style of styles) for (const gesture of gestures) {
      const presentation = new AvatarPresentation(expressions);
      for (let frame = 0; frame <= 180; frame++) {
        const values = presentation.advance({ time: frame / 60, delta: 1 / 60, behavior: 'speaking',
          delivery: { id: `${style}:${gesture}`, replyKey: 'verification', style, gesture },
          gaze: { x: Math.sin(frame), y: Math.cos(frame) }, blink: frame % 20 < 2 ? 0 : 1,
          lip: { open: frame % 3 / 2, form: frame % 3 - 1 } });
        winding(model, baseline, pose(model, values));
        combinationFrames++;
      }
    }
    const coreReport = { consistency: true, sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
      parameterCount: 19, parameterChecks };
    const packageReport = checkPackage(directory, coreReport, { poster });
    return { ...packageReport, blendShapeCount, ordinaryParameterCount: 19 - blendShapeCount,
      stableContract: stable, styleGestureCombinations: 35, combinationFrames, parameterChecks,
      officialEditorVerificationIncluded: false, visualAndEditorReviewRequired: true };
  } finally {
    model?.release();
    moc._release();
  }
}

/** 海报选项只声明资产契约，不推断包版本；stable 与官方工程门仍独立于图片检查。 */
async function main() {
  const args = process.argv.slice(2);
  const index = args.indexOf('--model-dir');
  const directory = index < 0 ? path.join(repository, 'web/public/avatar/xiaoya') : args[index + 1];
  if (!directory || directory.startsWith('--')) throw new Error('必须指定资产包目录');
  const posterIndex = args.indexOf('--poster');
  const poster = posterIndex < 0 ? currentPoster : args[posterIndex + 1];
  if (!poster || poster.startsWith('--')) throw new Error('必须指定海报文件');
  const report = await validateModel(path.resolve(directory), { stable: args.includes('--stable'), poster });
  const outputIndex = args.indexOf('--output');
  if (outputIndex >= 0) {
    const output = args[outputIndex + 1];
    if (!output || output.startsWith('--')) throw new Error('必须指定报告文件');
    fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true });
    fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
  }
  console.log(JSON.stringify(report, null, 2));
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url)
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
