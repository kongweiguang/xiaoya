import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { inflateSync } from 'node:zlib';
import { AvatarPresentation, parseAvatarExpression } from '../lib/avatar/presentation.ts';

const web = path.resolve(fileURLToPath(new URL('../', import.meta.url)));
const repository = path.dirname(web);
const directory = path.join(web, 'public/avatar/xiaoya');
const corePath = path.join(web, 'public/avatar/vendor/live2dcubismcore.min.js');
const styles = ['neutral', 'happy', 'gentle', 'concerned', 'curious', 'shy', 'surprised'];
const gestures = ['nod', 'tilt', 'wave', 'shy', 'shake'];
const primaryParameters = {
  nod: 'ParamAngleY',
  tilt: 'ParamAngleZ',
  wave: 'ParamArmL',
  shy: 'ParamArmL',
  shake: 'ParamAngleX',
};
const minimumPrimaryPeaks = { nod: 5, tilt: 8, wave: 0.65, shy: 0.45, shake: 8 };
const framework = await build({
  stdin: {
    contents: `
      export { CubismFramework, LogLevel } from './lib/avatar/vendor/cubism/src/live2dcubismframework.js';
      export { CubismMoc } from './lib/avatar/vendor/cubism/src/model/cubismmoc.js';
      export { CubismPhysics } from './lib/avatar/vendor/cubism/src/physics/cubismphysics.js';
      // 官方框架的 dispose 通过该模块注册静态释放函数；只导入，不实例化渲染器或创建 WebGL。
      import './lib/avatar/vendor/cubism/src/rendering/cubismrenderer_webgl.js';
    `,
    resolveDir: web,
  },
  absWorkingDir: web,
  bundle: true,
  write: false,
  metafile: true,
  format: 'iife',
  globalName: 'AvatarPhysicsKit',
  platform: 'browser',
});

/** 指纹约束当前真实输入，候选报告不能拿过去的合成器或表情资源代替这一轮代码。 */
function fingerprint(file) {
  const bytes = readFileSync(file);
  return {
    file: path.relative(repository, file).replaceAll('\\', '/'),
    bytes: bytes.length,
    sha256: createHash('sha256').update(bytes).digest('hex'),
  };
}

/** 读取 manifest 的完整资源闭包及实际后备图，不把同目录保留的旧模型混进当前包。 */
function readInputs(mocFile, textureFile) {
  const manifestFile = path.join(directory, 'xiaoya.model3.json');
  const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
  const refs = manifest.FileReferences;
  const files = new Set([
    'xiaoya.model3.json',
    refs.Moc,
    ...refs.Textures,
    refs.Physics,
    refs.DisplayInfo,
    refs.MotionSync,
    'concept-v1/poster.png',
  ]);
  const expressions = {};
  for (const reference of refs.Expressions) {
    files.add(reference.File);
    expressions[reference.Name] = parseAvatarExpression(
      JSON.parse(readFileSync(path.join(directory, reference.File), 'utf8'))
    );
  }
  for (const motions of Object.values(refs.Motions)) {
    for (const motion of motions) files.add(motion.File);
  }
  const formalFiles = [];
  for (const file of files) {
    assert.equal(typeof file, 'string');
    const resolved = path.resolve(directory, file);
    assert.ok(resolved.startsWith(directory + path.sep), 'manifest资源不能越出模型目录');
    formalFiles.push(resolved);
  }
  const targetMoc = mocFile ?? path.join(directory, refs.Moc);
  const targetTexture = textureFile ?? path.join(directory, refs.Textures[0]);
  const dependencies = new Set([
    ...formalFiles,
    corePath,
    targetMoc,
    targetTexture,
    path.join(web, 'lib/avatar/presentation.ts'),
    path.join(web, 'lib/avatar/delivery.ts'),
    path.join(web, 'lib/avatar/live2d-runtime.ts'),
    fileURLToPath(import.meta.url),
  ]);
  for (const file of Object.keys(framework.metafile.inputs)) {
    if (file !== '<stdin>') dependencies.add(path.resolve(web, file));
  }
  const physicsBytes = readFileSync(path.join(directory, refs.Physics));
  const physics = JSON.parse(physicsBytes.toString('utf8'));
  assert.equal(physics.PhysicsSettings.length, 1);
  assert.equal(physics.PhysicsSettings[0].Output.length, 1);
  for (const setting of physics.PhysicsSettings) {
    for (const output of setting.Output) assert.equal(output.Destination.Id, 'ParamLeafSwing');
  }
  assert.equal(
    fingerprint(targetTexture).sha256,
    fingerprint(path.join(directory, refs.Textures[0])).sha256,
    '候选必须沿用当前原画纹理，不能将像素差异误算为模型改良'
  );
  return {
    expressions,
    physicsBytes,
    physicsOutputWeight: physics.PhysicsSettings[0].Output[0].Weight / 100,
    targetMoc,
    targetTexture,
    formalFiles,
    dependencies: [...dependencies].sort(),
  };
}

/** 只解码现有 RGBA8 PNG 的透明度，不重绘纹理，也不引入图像编辑依赖。 */
function readAlpha(file) {
  const bytes = readFileSync(file);
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  assert.equal(bytes[24], 8);
  assert.equal(bytes[25], 6);
  assert.equal(bytes[28], 0);
  const parts = [];
  for (let offset = 8; offset < bytes.length; ) {
    const size = bytes.readUInt32BE(offset);
    if (bytes.subarray(offset + 4, offset + 8).toString() === 'IDAT') {
      parts.push(bytes.subarray(offset + 8, offset + 8 + size));
    }
    offset += size + 12;
  }
  const scan = inflateSync(Buffer.concat(parts));
  const pixels = Buffer.alloc(width * height * 4);
  const stride = width * 4;
  assert.equal(scan.length, height * (stride + 1));
  for (let y = 0; y < height; y++) {
    const filter = scan[y * (stride + 1)];
    for (let x = 0; x < stride; x++) {
      const a = x >= 4 ? pixels[y * stride + x - 4] : 0;
      const b = y > 0 ? pixels[(y - 1) * stride + x] : 0;
      const c = y > 0 && x >= 4 ? pixels[(y - 1) * stride + x - 4] : 0;
      let predict = 0;
      if (filter === 1) predict = a;
      else if (filter === 2) predict = b;
      else if (filter === 3) predict = Math.floor((a + b) / 2);
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        predict = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else assert.equal(filter, 0);
      pixels[y * stride + x] = (scan[y * (stride + 1) + 1 + x] + predict) & 255;
    }
  }
  return { width, height, pixels };
}

/** 框架与 Core 共用独立 VM，每次验收有自己的 ID 管理器，不污染其它 Node 测试或依赖 GPU。 */
async function loadKit(inputs) {
  const context = vm.createContext({
    require: createRequire(corePath),
    __dirname: path.dirname(corePath),
    __filename: corePath,
    process,
    Buffer,
    console,
    WebAssembly,
    setTimeout,
    clearTimeout,
    TextDecoder,
    TextEncoder,
    ArrayBuffer,
    Uint8Array,
    Float32Array,
    Int32Array,
    Uint32Array,
    Int16Array,
    Uint16Array,
  });
  context.global = context;
  vm.runInContext(readFileSync(corePath, 'utf8'), context, { filename: corePath });
  // 等待异步 WASM 实例可调用；只等待初始化，不重试或吞掉后续模型/物理失败。
  let version;
  for (let attempt = 0; attempt < 500; attempt++) {
    try {
      version = context.Live2DCubismCore.Version.csmGetVersion();
      if (version > 0) break;
    } catch {
      // 初始化之前公开版本 API 尚不可调用，有限等待防止测试无限挂起。
    }
    await delay(10);
  }
  assert.ok(version > 0, '官方Core未完成初始化');
  vm.runInContext(framework.outputFiles[0].text, context);
  const kit = context.AvatarPhysicsKit;
  const sdkErrors = [];
  /** 官方错误不能因为无渲染窗口而被静默忽略，结束时统一要求没有错误。 */
  function recordSdkError(message) {
    sdkErrors.push(String(message));
  }
  assert.equal(
    kit.CubismFramework.startUp({
      loggingLevel: kit.LogLevel.LogLevel_Error,
      logFunction: recordSdkError,
    }),
    true
  );
  try {
    kit.CubismFramework.initialize();
    const bytes = readFileSync(inputs.targetMoc);
    const moc = kit.CubismMoc.create(toArrayBuffer(bytes), true);
    assert.ok(moc, '官方Core必须接受实际MOC');
    return { ...kit, moc, version, sdkErrors };
  } catch (error) {
    kit.CubismFramework.dispose();
    kit.CubismFramework.cleanUp();
    throw error;
  }
}

/** Node Buffer 可能带池偏移，切出精确字节范围才能向官方二进制解析 API 传入真实资源。 */
function toArrayBuffer(bytes) {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

/** 独立轨迹新建物理实例；不能用 initialize 清缓存或每帧重建来掩盖物理累积错误。 */
function createTrack(kit, inputs) {
  const model = kit.moc.createModel();
  assert.ok(model);
  let physics;
  try {
    assert.equal(model.getParameterCount(), 19);
    assert.equal(model.getDrawableCount(), 1);
    assert.equal(model.getDrawableVertexCount(0), 6561);
    physics = kit.CubismPhysics.create(
      toArrayBuffer(inputs.physicsBytes),
      inputs.physicsBytes.length
    );
    assert.ok(physics);
    const index = {};
    for (let i = 0; i < model.getParameterCount(); i++) {
      index[model.getParameterId(i).getString().s] = i;
    }
    return {
      model,
      physics,
      index,
      physicsOutputWeight: inputs.physicsOutputWeight,
      presentation: new AvatarPresentation(inputs.expressions),
    };
  } catch (error) {
    physics?.release();
    kit.moc.deleteModel(model);
    throw error;
  }
}

/** 参数必须绑定真实 Core 索引，SDK 自动创建的虚拟未知参数不能让缺失绑定假通过。 */
function setParameter(track, name, value) {
  const index = track.index[name];
  assert.ok(Number.isInteger(index) && index >= 0 && index < track.model.getParameterCount(), name);
  track.model.setParameterValueByIndex(index, value);
}

/** 全部三角形都检查，包括透明区域；面积必须严格保持原绕序，不接受负容差来隐藏翻折。 */
function triangleArea(vertices, indices, index) {
  const a = indices[index] * 2;
  const b = indices[index + 1] * 2;
  const c = indices[index + 2] * 2;
  return (
    (vertices[b] - vertices[a]) * (vertices[c + 1] - vertices[a + 1]) -
    (vertices[c] - vertices[a]) * (vertices[b + 1] - vertices[a + 1])
  );
}

/** 参考姿态来自当前被测MOC，不借用正式模型的拓扑给另一个候选判定安全。 */
function referenceGeometry(track, texture) {
  setParameter(track, 'ParamMouthOpenY', 1);
  track.model.update();
  const vertices = Float32Array.from(track.model.getDrawableVertices(0));
  const indices = Uint16Array.from(track.model.getDrawableVertexIndices(0));
  const signs = [];
  const masks = { visible: [], head: [], armL: [], armR: [] };
  for (let point = 0; point < vertices.length / 2; point++) {
    const x = (point % 81) * 16;
    const y = Math.floor(point / 81) * 16;
    assert.ok(Math.abs(vertices[point * 2] - (x - 640) / 640) < 1e-5);
    assert.ok(Math.abs(vertices[point * 2 + 1] - (640 - y) / 640) < 1e-5);
    const tx = Math.min(texture.width - 1, Math.floor((x / 1280) * texture.width));
    const ty = Math.min(texture.height - 1, Math.floor((y / 1280) * texture.height));
    if (texture.pixels[(ty * texture.width + tx) * 4 + 3] < 128) continue;
    masks.visible.push(point);
    if (y >= 230 && y <= 745 && x >= 250 && x <= 1040) masks.head.push(point);
    if (y >= 773 && y <= 1097 && x >= 283 && x <= 493) masks.armL.push(point);
    if (y >= 773 && y <= 1097 && x >= 755 && x <= 965) masks.armR.push(point);
  }
  for (let index = 0; index < indices.length; index += 3) {
    const signed = triangleArea(vertices, indices, index);
    assert.notEqual(signed, 0, '参考拓扑不能退化');
    signs.push(Math.sign(signed));
  }
  assert.ok(masks.visible.length > 0);
  const pixelsPerModelUnit =
    458 / Math.max(track.model.getCanvasWidth(), track.model.getCanvasHeight());
  return { indices, signs, masks, pixelsPerModelUnit };
}

/** 几何安全必须逐帧验证，不能仅根据参数在范围内推定复杂组合没有撕裂或翻面。 */
function assertModelSafe(track, reference, statistics, label) {
  const model = track.model;
  for (let i = 0; i < model.getParameterCount(); i++) {
    const value = model.getParameterValueByIndex(i);
    assert.ok(Number.isFinite(value), `${label}: parameter ${i} 非有限`);
    assert.ok(value >= model.getParameterMinimumValue(i) - 1e-6, `${label}: parameter ${i} 下越界`);
    assert.ok(value <= model.getParameterMaximumValue(i) + 1e-6, `${label}: parameter ${i} 上越界`);
  }
  const vertices = model.getDrawableVertices(0);
  for (const value of vertices) {
    assert.ok(Number.isFinite(value), `${label}: 非有限顶点`);
    // 当前源画布为[-1,1]；额外半幅只作失控保护，不把通过该宽界限当成视觉合格。
    assert.ok(Math.abs(value) <= 1.5, `${label}: 顶点超过原画布的1.5倍半幅`);
    statistics.maximumAbsoluteVertexCoordinate = Math.max(
      statistics.maximumAbsoluteVertexCoordinate,
      Math.abs(value)
    );
  }
  for (let index = 0; index < reference.indices.length; index += 3) {
    const signed = triangleArea(vertices, reference.indices, index) * reference.signs[index / 3];
    if (!(Number.isFinite(signed) && signed > 0)) {
      assert.fail(`${label}: 三角${index / 3}翻折或退化，signedArea=${signed}`);
    }
    statistics.minimumTriangleArea = Math.min(statistics.minimumTriangleArea, signed);
  }
  statistics.checkedFrames += 1;
  return vertices;
}

/** 严格复用宿主合成顺序；物理前后叶片差值排除“正弦基线非零”假冒真实物理的情况。 */
function advanceTrack(track, input, reference, statistics, label) {
  const values = track.presentation.advance(input);
  for (const [name, value] of Object.entries(values)) {
    if (name !== 'ParamMouthOpenY' && name !== 'ParamMouthForm') setParameter(track, name, value);
  }
  const beforeLeaf = track.model.getParameterValueByIndex(track.index.ParamLeafSwing);
  track.physics.evaluate(track.model, Math.max(0, Math.min(input.delta, 0.05)));
  const afterLeaf = track.model.getParameterValueByIndex(track.index.ParamLeafSwing);
  for (const [name, value] of Object.entries(values)) {
    if (name === 'ParamMouthOpenY' || name === 'ParamMouthForm' || name === 'ParamLeafSwing')
      continue;
    assert.ok(
      Math.abs(track.model.getParameterValueByIndex(track.index[name]) - value) < 1e-6,
      `${label}: 物理越权改写${name}`
    );
  }
  setParameter(track, 'ParamMouthOpenY', values.ParamMouthOpenY);
  setParameter(track, 'ParamMouthForm', values.ParamMouthForm);
  track.model.update();
  for (const [name, expected] of [
    ['ParamMouthOpenY', input.lip?.open ?? 0],
    ['ParamMouthForm', input.lip?.form ?? 0],
  ]) {
    assert.ok(
      Math.abs(track.model.getParameterValueByIndex(track.index[name]) - expected) < 1e-6,
      `${label}: ${name}必须来自当前真实口型输入`
    );
  }
  if (input.blink === 0) {
    assert.equal(track.model.getParameterValueByIndex(track.index.ParamEyeLOpen), 0);
    assert.equal(track.model.getParameterValueByIndex(track.index.ParamEyeROpen), 0);
  }
  const physicsDelta = Math.abs(afterLeaf - beforeLeaf);
  // 还必须超过“输出恒为零、只按weight削弱基线”的结果，证明摆锤真正产生动态输出。
  const physicsContribution =
    input.delta > 0 ? Math.abs(afterLeaf - beforeLeaf * (1 - track.physicsOutputWeight)) : 0;
  statistics.maximumPhysicsDelta = Math.max(statistics.maximumPhysicsDelta, physicsDelta);
  if (physicsContribution > statistics.maximumPhysicsContribution) {
    const parameters = {};
    for (const [name, index] of Object.entries(track.index)) {
      parameters[name] = track.model.getParameterValueByIndex(index);
    }
    statistics.physicsSample = {
      label,
      beforeLeaf,
      afterLeaf,
      zeroOutputLeaf: beforeLeaf * (1 - track.physicsOutputWeight),
      parameters,
    };
  }
  statistics.maximumPhysicsContribution = Math.max(
    statistics.maximumPhysicsContribution,
    physicsContribution
  );
  return {
    values,
    physicsDelta,
    physicsContribution,
    vertices: assertModelSafe(track, reference, statistics, label),
  };
}

/** 可听包络、视线和口型组合在两轨完全相同，只把手势及其有意的轻摆衰减作为实验变量。 */
function frameInput(time, delta, frame, delivery) {
  return {
    time,
    delta,
    behavior: 'speaking',
    delivery,
    gaze: { x: 0.2 * Math.sin(time), y: 0.1 * Math.cos(time * 0.8) },
    blink: [0, 0.5, 1][Math.floor(frame / 9) % 3],
    lip: { open: [0, 0.55, 1][frame % 3], form: [-1, 0, 1][Math.floor(frame / 3) % 3] },
  };
}

/** 段与回复身份都显式提供，缺replyKey必须在非零动作断言处失败，不兼容旧诊断伪输入。 */
function cue(id, style, gesture, reply = 'reply-a') {
  return { id, replyKey: JSON.stringify(['physics-evidence-v2', reply]), style, gesture };
}

/** 只量化实际可见纹理顶点的差分；像素位移不是表情语义或渲染美观已经通过的证据。 */
function compareGeometry(left, right, reference, peaks) {
  for (const [name, points] of Object.entries(reference.masks)) {
    for (const point of points) {
      peaks[name] = Math.max(
        peaks[name] ?? 0,
        Math.hypot(left[point * 2] - right[point * 2], left[point * 2 + 1] - right[point * 2 + 1]) *
          reference.pixelsPerModelUnit
      );
    }
  }
}

/** 每轨物理/模型按公开生命周期独立释放，不能用复用旧缓存减少测试开销。 */
function releaseTrack(kit, track) {
  if (!track) return;
  track.physics.release();
  kit.moc.deleteModel(track.model);
}

/** 在真实动态轨迹的同一冻结姿态只替换叶片参数，证明摆锤输出绑定到了可见网格，而不只改了数字。 */
function verifyPhysicsGeometry(kit, inputs, reference, sample) {
  assert.ok(sample, '必须先捕获真实物理轨迹中的非零摆锤输出');
  const track = createTrack(kit, inputs);
  const statistics = {
    checkedFrames: 0,
    minimumTriangleArea: Infinity,
    maximumAbsoluteVertexCoordinate: 0,
  };
  try {
    for (const [name, value] of Object.entries(sample.parameters)) setParameter(track, name, value);
    const frames = {};
    for (const [name, value] of Object.entries({
      beforePhysics: sample.beforeLeaf,
      zeroPendulumOutput: sample.zeroOutputLeaf,
      afterPhysics: sample.afterLeaf,
    })) {
      setParameter(track, 'ParamLeafSwing', value);
      track.model.update();
      frames[name] = Float32Array.from(
        assertModelSafe(track, reference, statistics, `physics-geometry/${name}`)
      );
    }
    const totalEffect = {};
    const pendulumEffect = {};
    compareGeometry(frames.beforePhysics, frames.afterPhysics, reference, totalEffect);
    compareGeometry(frames.zeroPendulumOutput, frames.afterPhysics, reference, pendulumEffect);
    assert.ok(totalEffect.visible > 1e-5, '物理参数变化必须绑定到真实可见网格');
    assert.ok(pendulumEffect.visible > 1e-5, '真实摆锤输出必须超过只削弱基线的几何结果');
    return {
      sourceFrame: sample.label,
      additionalFrozenPoses: statistics.checkedFrames,
      minimumTriangleArea: statistics.minimumTriangleArea,
      maximumAbsoluteVertexCoordinate: statistics.maximumAbsoluteVertexCoordinate,
      totalEffectPixelsAt458: totalEffect,
      pendulumOnlyEffectPixelsAt458: pendulumEffect,
    };
  } finally {
    releaseTrack(kit, track);
  }
}

/** 七风格五动作经过连续口型格；害羞须双臂内收，摇头须真实左右两向，不能仅凭任意非零值通过。 */
function runGesture(kit, inputs, reference, statistics, style, gesture, timing = '60fps') {
  const action = createTrack(kit, inputs);
  let control;
  const combinations = new Set();
  const pixels = {};
  let time = 0;
  let primaryPeak = 0;
  let physicsPeak = 0;
  let contributionPeak = 0;
  let inwardLeftPeak = 0;
  let inwardRightPeak = 0;
  let downwardPeak = 0;
  let shakeLeftPeak = 0;
  let shakeRightPeak = 0;
  try {
    control = createTrack(kit, inputs);
    for (let frame = 0; frame < 150; frame++) {
      let delta = timing === '30fps' ? 1 / 30 : 1 / 60;
      if (timing === 'pause' && frame === 61) delta = 1;
      if (timing === 'pause' && frame === 70) delta = 0;
      if (timing === 'pause' && frame === 71) delta = -1 / 60;
      if (timing === 'pause' && frame >= 80) delta = frame % 2 ? 1 / 30 : 0.05;
      if (frame > 0) time += Math.max(0, delta);
      const label = `${style}/${gesture}/${timing}/${frame}`;
      const input = frameInput(time, delta, frame, cue('action', style, gesture));
      const baseline = advanceTrack(
        control,
        { ...input, delivery: cue('baseline', style, 'none') },
        reference,
        statistics,
        `${label}/baseline`
      );
      const changed = advanceTrack(action, input, reference, statistics, `${label}/action`);
      const parameter = primaryParameters[gesture];
      primaryPeak = Math.max(
        primaryPeak,
        Math.abs(changed.values[parameter] - baseline.values[parameter])
      );
      physicsPeak = Math.max(physicsPeak, changed.physicsDelta);
      contributionPeak = Math.max(contributionPeak, changed.physicsContribution);
      if (gesture === 'shy') {
        inwardLeftPeak = Math.max(
          inwardLeftPeak,
          baseline.values.ParamArmL - changed.values.ParamArmL
        );
        inwardRightPeak = Math.max(
          inwardRightPeak,
          baseline.values.ParamArmR - changed.values.ParamArmR
        );
        downwardPeak = Math.max(
          downwardPeak,
          baseline.values.ParamAngleY - changed.values.ParamAngleY
        );
      }
      if (gesture === 'shake') {
        shakeLeftPeak = Math.max(
          shakeLeftPeak,
          baseline.values.ParamAngleX - changed.values.ParamAngleX
        );
        shakeRightPeak = Math.max(
          shakeRightPeak,
          changed.values.ParamAngleX - baseline.values.ParamAngleX
        );
      }
      combinations.add(`${input.blink}:${input.lip.open}:${input.lip.form}`);
      compareGeometry(baseline.vertices, changed.vertices, reference, pixels);
    }
    assert.equal(combinations.size, 27, '眨眼/开合/嘴形必须覆盖完整3×3×3格');
    assert.ok(primaryPeak > minimumPrimaryPeaks[gesture], `${style}/${gesture}: 主动作未真正启动`);
    assert.ok(physicsPeak > 1e-5, `${style}/${gesture}: 未观察到真实物理增量`);
    assert.ok(contributionPeak > 1e-5, `${style}/${gesture}: 摆锤输出不能恒为零`);
    if (gesture === 'shy') {
      assert.ok(inwardLeftPeak > 0.45 && inwardRightPeak > 0.45, '害羞必须双臂向内');
      assert.ok(downwardPeak > 12, '害羞必须保留低头主动作');
    }
    if (gesture === 'shake') {
      assert.ok(shakeLeftPeak > 8 && shakeRightPeak > 8, '摇头必须向左右两侧运动');
    }
    return {
      style,
      gesture,
      timing,
      primaryPeak,
      maximumPhysicsDelta: physicsPeak,
      maximumPhysicsContribution: contributionPeak,
      ...(gesture === 'shy' ? { inwardLeftPeak, inwardRightPeak, downwardPeak } : {}),
      ...(gesture === 'shake' ? { shakeLeftPeak, shakeRightPeak } : {}),
      pixelsAt458: pixels,
    };
  } finally {
    releaseTrack(kit, action);
    releaseTrack(kit, control);
  }
}

/** 挥手、害羞与摇头取消后均不得复活；中性面部隔离动作，头部下限高于音频微动，防止假启动。 */
function runCancellation(kit, inputs, reference, statistics, gesture = 'wave') {
  const action = createTrack(kit, inputs);
  let control;
  let initialPeak = 0;
  let restartedPeak = 0;
  try {
    control = createTrack(kit, inputs);
    for (let frame = 0; frame < 240; frame++) {
      const time = frame / 60;
      let delivery = frame < 48 ? cue('first', 'neutral', gesture) : null;
      let expected = delivery;
      if (frame >= 20 && frame < 30) delivery = cue('pending-none', 'neutral', 'none');
      if (frame >= 30 && frame < 48) delivery = cue('same-kind', 'neutral', gesture);
      if (frame >= 72 && frame < 81) delivery = cue('late-none', 'neutral', 'none');
      if (frame >= 81) delivery = cue('late-gesture', 'neutral', gesture);
      if (frame >= 150) delivery = expected = cue('new-reply', 'neutral', gesture, 'reply-b');
      const input = frameInput(time, 1 / 60, frame, delivery);
      // 取消后的短暂静音验证嘴马上归零，而不是依据 speaking 或尚在摆动的叶片伪造口型。
      if (frame >= 48 && frame < 65) input.lip = null;
      const baseline = advanceTrack(
        control,
        { ...input, delivery: expected },
        reference,
        statistics,
        `cancel/${gesture}/${frame}/baseline`
      );
      const changed = advanceTrack(
        action,
        input,
        reference,
        statistics,
        `cancel/${gesture}/${frame}/action`
      );
      assert.deepEqual(changed.values, baseline.values, `cancel/${frame}: 迟到段不得改变合成姿态`);
      assert.deepEqual(
        changed.vertices,
        baseline.vertices,
        `cancel/${frame}: 物理轨迹发生额外重启`
      );
      const primary = primaryParameters[gesture];
      if (frame < 48) initialPeak = Math.max(initialPeak, Math.abs(changed.values[primary]));
      if (frame >= 110 && frame < 150) {
        assert.ok(Math.abs(changed.values.ParamArmL) < 1e-5);
        assert.ok(Math.abs(changed.values.ParamArmR) < 1e-5);
      }
      if (frame >= 150) restartedPeak = Math.max(restartedPeak, Math.abs(changed.values[primary]));
    }
    assert.ok(initialPeak > (gesture === 'shake' ? 8 : 0.4));
    assert.ok(restartedPeak > minimumPrimaryPeaks[gesture], '新回复应重新获得同款动作许可');
    return { gesture, cancelledReplyDidNotRevive: true, initialPeak, newReplyPeak: restartedPeak };
  } finally {
    releaseTrack(kit, action);
    releaseTrack(kit, control);
  }
}

/** 七风格五动作共用正式Core与物理，另加害羞/摇头取消和长帧；不冒充Editor或浏览器视觉门。 */
export async function verifyPhysicsModel({ mocFile, textureFile } = {}) {
  const inputs = readInputs(mocFile, textureFile);
  const before = inputs.dependencies.map(fingerprint);
  const kit = await loadKit(inputs);
  const statistics = {
    checkedFrames: 0,
    minimumTriangleArea: Infinity,
    maximumAbsoluteVertexCoordinate: 0,
    maximumPhysicsDelta: 0,
    maximumPhysicsContribution: 0,
  };
  try {
    const referenceTrack = createTrack(kit, inputs);
    let reference;
    try {
      reference = referenceGeometry(referenceTrack, readAlpha(inputs.targetTexture));
    } finally {
      releaseTrack(kit, referenceTrack);
    }
    const trajectories = [];
    for (const style of styles) {
      for (const gesture of gestures) {
        trajectories.push(runGesture(kit, inputs, reference, statistics, style, gesture));
      }
    }
    for (const timing of ['30fps', 'pause']) {
      trajectories.push(runGesture(kit, inputs, reference, statistics, 'happy', 'wave', timing));
      trajectories.push(runGesture(kit, inputs, reference, statistics, 'shy', 'shy', timing));
      trajectories.push(
        runGesture(kit, inputs, reference, statistics, 'surprised', 'shake', timing)
      );
    }
    const cancellation = runCancellation(kit, inputs, reference, statistics);
    const shyCancellation = runCancellation(kit, inputs, reference, statistics, 'shy');
    const shakeCancellation = runCancellation(kit, inputs, reference, statistics, 'shake');
    const physicsGeometry = verifyPhysicsGeometry(kit, inputs, reference, statistics.physicsSample);
    assert.deepEqual(
      inputs.dependencies.map(fingerprint),
      before,
      '验收期间输入文件变动，结果必须作废'
    );
    assert.deepEqual(kit.sdkErrors, [], '官方SDK错误不能被测试吞掉');
    return {
      evidenceVersion: 'reply-physics-presets-v4',
      checkedAt: new Date().toISOString(),
      nodeVersion: process.version,
      coreVersion: kit.version,
      passed: true,
      physicsIncluded: true,
      rendererIncluded: false,
      officialEditorVerificationIncluded: false,
      targetMoc: fingerprint(inputs.targetMoc),
      targetTexture: fingerprint(inputs.targetTexture),
      currentFormalPackage: inputs.formalFiles.map(fingerprint),
      inputFingerprints: before,
      geometry: {
        parameterCount: 19,
        drawableCount: 1,
        vertexCount: 6561,
        canvasPixels: 458,
        visibleVertices: reference.masks.visible.length,
      },
      ...statistics,
      trajectories,
      cancellation,
      shyCancellation,
      shakeCancellation,
      physicsGeometry,
    };
  } finally {
    kit.moc.release();
    kit.CubismFramework.dispose();
    kit.CubismFramework.cleanUp();
  }
}

/** 七风格五动作沿用严格物理几何约束，并输出当前正式模型实测幅度，不拿非零替代视觉自然度。 */
test('正式模型真实物理叠加覆盖七风格五动作、口型组合、取消及长帧', async (context) => {
  const report = await verifyPhysicsModel();
  assert.equal(report.passed, true);
  assert.equal(report.physicsIncluded, true);
  assert.equal(report.trajectories.length, 41);
  assert.equal(report.checkedFrames, 13740);
  assert.ok(report.minimumTriangleArea > 0);
  assert.ok(report.maximumPhysicsDelta > 1e-5);
  assert.ok(report.maximumPhysicsContribution > 1e-5);
  assert.equal(report.physicsGeometry.additionalFrozenPoses, 3);
  assert.ok(report.physicsGeometry.pendulumOnlyEffectPixelsAt458.visible > 1e-5);
  assert.equal(report.shyCancellation.cancelledReplyDidNotRevive, true);
  assert.equal(report.shakeCancellation.cancelledReplyDidNotRevive, true);
  for (const trajectory of report.trajectories) {
    if (trajectory.timing !== '60fps') continue;
    if (
      (trajectory.style === 'shy' && trajectory.gesture === 'shy') ||
      (trajectory.style === 'surprised' && trajectory.gesture === 'shake')
    ) {
      context.diagnostic(
        `${trajectory.style}/${trajectory.gesture} @458px ${JSON.stringify(trajectory.pixelsAt458)}；主动作参数峰值${trajectory.primaryPeak.toFixed(3)}，非视觉验收`
      );
    }
  }
  context.diagnostic(`13740动态帧及3冻结对照无翻折；最小二倍三角面积${report.minimumTriangleArea}`);
});
