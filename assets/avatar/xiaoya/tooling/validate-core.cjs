const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const crypto = require('node:crypto');

/** 官方 Core 同时验证嘴型与新增面部连续形变；保留全部旧门，不能把参数存在等同于可见表情。 */
async function main() {
  const coreFile = path.resolve(process.argv[3] || 'web/public/avatar/vendor/live2dcubismcore.min.js');
  const context = { require, __dirname: path.dirname(coreFile), __filename: coreFile, process, Buffer, console, WebAssembly, setTimeout, clearTimeout, TextDecoder, TextEncoder, ArrayBuffer, Uint8Array, Float32Array, Int32Array, Uint32Array, Int16Array, Uint16Array };
  context.global = context;
  vm.runInNewContext(fs.readFileSync(coreFile, 'utf8'), context, { filename: coreFile });
  const Live2DCubismCore = context.Live2DCubismCore;
  await new Promise((resolve) => setTimeout(resolve, 50));
  const file = path.resolve(process.argv[2]);
  const buffer = fs.readFileSync(file);
  const bytes = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
  const moc = Live2DCubismCore.Moc.fromArrayBuffer(bytes);
  if (!moc) throw new Error('Official Core could not revive MOC3');
  const consistency = moc.hasMocConsistency(bytes);
  if (consistency !== 1) throw new Error('Official Core rejected MOC3 consistency');
  const model = Live2DCubismCore.Model.fromMoc(moc);
  if (!model) throw new Error('Official Core could not instantiate model');
  model.update();
  const before = model.drawables.vertexPositions.map((vertices) => Array.from(vertices));
  const mouthIndex = model.parameters.ids.indexOf('ParamMouthOpenY');
  model.parameters.values[mouthIndex] = 1;
  model.update();
  const after = model.drawables.vertexPositions.map((vertices) => Array.from(vertices));
  const changed = before.some((vertices, index) => vertices.some((value, vertex) => Math.abs(value - after[index][vertex]) > 0.001));
  if (!changed) throw new Error('ParamMouthOpenY did not change real Core geometry');
  const parameterChecks = {};
  for (let parameter = 0; parameter < model.parameters.count; parameter++) {
    model.parameters.values.set(model.parameters.defaultValues);
    model.update();
    const neutral = model.drawables.vertexPositions.map((vertices) => Array.from(vertices));
    model.parameters.values[parameter] = model.parameters.defaultValues[parameter] === model.parameters.maximumValues[parameter]
      ? model.parameters.minimumValues[parameter] : model.parameters.maximumValues[parameter];
    model.update();
    const movesVertices = neutral.some((vertices, index) => vertices.some((value, vertex) => Math.abs(value - model.drawables.vertexPositions[index][vertex]) > 0.00001));
    parameterChecks[model.parameters.ids[parameter]] = {
      min: model.parameters.minimumValues[parameter],
      max: model.parameters.maximumValues[parameter],
      default: model.parameters.defaultValues[parameter],
      movesVertices,
    };
  }
  model.parameters.values.set(model.parameters.defaultValues);
  model.update();
  if (Object.values(parameterChecks).some((check) => !check.movesVertices)) {
    throw new Error('A declared model parameter has no visible geometric binding');
  }
  let presentationRigChecks = null;
  if (model.parameters.ids.includes('ParamEyeSmile')) {
    for (const id of ['ParamBrowLAngle', 'ParamBrowRAngle'])
      if (!model.parameters.ids.includes(id)) throw new Error('Incomplete presentation rig: ' + id);
    let combinations = 0;
    let minimumTriangleArea = Infinity;
    for (const smile of [0, 0.3, 1]) {
      for (const openness of [0, 0.1, 0.5, 1]) {
        for (const angle of [-1, 0, 1]) {
          model.parameters.values.set(model.parameters.defaultValues);
          for (const [id, value] of Object.entries({ ParamEyeSmile: smile, ParamEyeLOpen: openness, ParamEyeROpen: openness, ParamBrowLAngle: angle, ParamBrowRAngle: -angle }))
            model.parameters.values[model.parameters.ids.indexOf(id)] = value;
          model.update();
          for (const id of ['EyeWhiteL', 'EyeWhiteR', 'PupilL', 'PupilR', 'BrowL', 'BrowR']) {
            const drawable = model.drawables.ids.indexOf(id);
            const vertices = model.drawables.vertexPositions[drawable];
            const indices = model.drawables.indices[drawable];
            if (vertices.length < 18 || Array.from(vertices).some(
              /** 连续弧线需要实际细分网格，NaN 不应被截图背景掩盖。 */
              value => !Number.isFinite(value))) throw new Error('Invalid expression mesh: ' + id);
            for (let triangle = 0; triangle < indices.length; triangle += 3) {
              const a = indices[triangle] * 2, b = indices[triangle + 1] * 2, c = indices[triangle + 2] * 2;
              const area = (vertices[b] - vertices[a]) * (vertices[c + 1] - vertices[a + 1]) - (vertices[c] - vertices[a]) * (vertices[b + 1] - vertices[a + 1]);
              // 闭眼和笑眼瞳孔允许退化为线，但不允许翻面或穿插。
              if (area < -0.00000001) throw new Error('Expression triangle reverses: ' + JSON.stringify({ id, smile, openness, angle, area }));
              minimumTriangleArea = Math.min(minimumTriangleArea, area);
            }
          }
          combinations++;
        }
      }
    }
    model.parameters.values.set(model.parameters.defaultValues);
    model.parameters.values[model.parameters.ids.indexOf('ParamEyeSmile')] = 1;
    model.update();
    const eyeCurvatures = {};
    for (const id of ['EyeWhiteL', 'EyeWhiteR']) {
      const vertices = model.drawables.vertexPositions[model.drawables.ids.indexOf(id)];
      const first = (vertices[1] + vertices[3]) / 2;
      const middle = (vertices[17] + vertices[19]) / 2;
      const last = (vertices[33] + vertices[35]) / 2;
      const curvature = (middle - (first + last) / 2) * model.canvasinfo.PixelsPerUnit;
      if (curvature < 15) throw new Error('Smile is only a flattened eye, not a curved mesh: ' + id);
      eyeCurvatures[id] = curvature;
    }
    model.parameters.values.set(model.parameters.defaultValues);
    model.update();
    const armIndex = model.drawables.ids.indexOf('ArmL');
    const restingArm = Array.from(model.drawables.vertexPositions[armIndex]);
    model.parameters.values[model.parameters.ids.indexOf('ParamArmL')] = 1;
    model.update();
    let armDisplacement = 0;
    for (let vertex = 0; vertex < restingArm.length; vertex++)
      armDisplacement = Math.max(armDisplacement, Math.abs(restingArm[vertex] - model.drawables.vertexPositions[armIndex][vertex]) * model.canvasinfo.PixelsPerUnit);
    if (armDisplacement < 70) throw new Error('Authored arm motion remains too small to see');
    presentationRigChecks = { combinations, minimumTriangleArea, eyeCurvatureSourcePixels: eyeCurvatures, armDisplacementSourcePixels: armDisplacement, passed: true };
  }
  const mouthShapeChecks = {};
  const mouthPoses = [
    ['closed', 0, 0], ['open', 1, 0], ['round', 1, -1], ['spread', 1, 1],
  ];
  for (const [pose, open, form] of mouthPoses) {
    model.parameters.values.set(model.parameters.defaultValues);
    model.parameters.values[model.parameters.ids.indexOf('ParamMouthOpenY')] = open;
    model.parameters.values[model.parameters.ids.indexOf('ParamMouthForm')] = form;
    model.update();
    const shapes = {};
    for (const id of ['MouthOuter', 'MouthInner']) {
      const index = model.drawables.ids.indexOf(id);
      if (index < 0) throw new Error('Missing mouth drawable ' + id);
      const coordinates = model.drawables.vertexPositions[index];
      const mouthXs = [];
      const mouthYs = [];
      for (let vertex = 0; vertex < coordinates.length; vertex += 2) {
        mouthXs.push(coordinates[vertex]);
        mouthYs.push(coordinates[vertex + 1]);
      }
      shapes[id] = {
        width: Math.max(...mouthXs) - Math.min(...mouthXs),
        height: Math.max(...mouthYs) - Math.min(...mouthYs),
      };
    }
    mouthShapeChecks[pose] = shapes;
  }
  for (const id of ['MouthOuter', 'MouthInner']) {
    const closed = mouthShapeChecks.closed[id];
    const open = mouthShapeChecks.open[id];
    const round = mouthShapeChecks.round[id];
    const spread = mouthShapeChecks.spread[id];
    // 机器人闭嘴保留一条可见唇线；要求压至张口高度的十分之一内，而非把纹理缩为退化零面积网格。
    if (!(closed.height < open.height * 0.1 && round.width < open.width && spread.width > open.width)) {
      throw new Error('Mouth geometry must support closed, open, round and spread poses: ' + id + ' ' + JSON.stringify(mouthShapeChecks));
    }
  }
  const mouthTransitionChecks = [];
  for (const form of [-1, 0, 1]) {
    let lastOuterHeight = -1;
    for (const open of [0, 0.001, 0.01, 0.02, 0.05, 0.25, 0.5, 1]) {
      model.parameters.values.set(model.parameters.defaultValues);
      model.parameters.values[model.parameters.ids.indexOf('ParamMouthOpenY')] = open;
      model.parameters.values[model.parameters.ids.indexOf('ParamMouthForm')] = form;
      model.update();
      const drawables = [];
      for (const id of ['MouthOuter', 'MouthInner']) {
        const index = model.drawables.ids.indexOf(id);
        const coordinates = model.drawables.vertexPositions[index];
        const indices = model.drawables.indices[index];
        const mouthYs = [];
        let zeroAreaTriangles = 0;
        if (Array.from(coordinates).some((value) => !Number.isFinite(value))) {
          throw new Error('Non-finite mouth vertices during closed-to-open transition');
        }
        for (let vertex = 1; vertex < coordinates.length; vertex += 2) mouthYs.push(coordinates[vertex]);
        for (let triangle = 0; triangle < indices.length; triangle += 3) {
          const a = indices[triangle] * 2;
          const b = indices[triangle + 1] * 2;
          const c = indices[triangle + 2] * 2;
          const area = (coordinates[b] - coordinates[a]) * (coordinates[c + 1] - coordinates[a + 1]) -
            (coordinates[c] - coordinates[a]) * (coordinates[b + 1] - coordinates[a + 1]);
          if (area < -0.00000001) throw new Error('Mouth triangle reverses during transition');
          if (Math.abs(area) < 0.00000001) zeroAreaTriangles++;
        }
        const height = Math.max(...mouthYs) - Math.min(...mouthYs);
        if (id === 'MouthOuter') {
          // 闭唇弧线的包围盒在极小开口会变化不足半个源像素；容差只排除肉眼可见的反向收缩。
          if (height + 0.0008 < lastOuterHeight) throw new Error('Mouth opening visibly shrinks during transition: ' + JSON.stringify({ open, form, height, lastOuterHeight }));
          lastOuterHeight = height;
        }
        drawables.push({ id, vertexCount: coordinates.length / 2, height, zeroAreaTriangles, finite: true });
      }
      mouthTransitionChecks.push({ open, form, drawables });
    }
  }
  model.parameters.values.set(model.parameters.defaultValues);
  model.update();
  const renderOrders = Array.from(model.drawables.renderOrders);
  if (new Set(renderOrders).size !== model.drawables.count || Math.min(...renderOrders) !== 0 || Math.max(...renderOrders) !== model.drawables.count - 1) {
    throw new Error('Core render orders must be a unique 0..drawableCount-1 permutation');
  }
  for (let drawable = 0; drawable < model.drawables.count; drawable++) {
    if (Array.from(model.drawables.vertexPositions[drawable]).some((value) => !Number.isFinite(value)) || Array.from(model.drawables.vertexUvs[drawable]).some((value) => !Number.isFinite(value) || value < 0 || value > 1)) {
      throw new Error('Drawable geometry or UVs are invalid');
    }
    if (model.drawables.textureIndices[drawable] !== 0 || model.drawables.opacities[drawable] !== 1) throw new Error('Expected one atlas and visible neutral drawables');
  }
  const xs = [];
  const ys = [];
  for (const vertices of model.drawables.vertexPositions) {
    for (let index = 0; index < vertices.length; index += 2) { xs.push(vertices[index]); ys.push(vertices[index + 1]); }
  }
  const report = {
    file,
    sha256: crypto.createHash('sha256').update(buffer).digest('hex'),
    officialCoreVersion: Live2DCubismCore.Version.csmGetVersion(),
    consistency: consistency === 1,
    parameterCount: model.parameters.count,
    drawableCount: model.drawables.count,
    renderOrders,
    drawOrders: Array.from(model.drawables.drawOrders),
    parameters: model.parameters.ids,
    canvas: model.canvasinfo,
    mouthOpenChangesVertices: changed,
    firstDrawableUVs: Array.from(model.drawables.vertexUvs[0]),
    drawableDetails: model.drawables.ids.map((id, index) => ({
      id, textureIndex: model.drawables.textureIndices[index], opacity: model.drawables.opacities[index],
      constantFlags: model.drawables.constantFlags[index], dynamicFlags: model.drawables.dynamicFlags[index],
      positions: Array.from(model.drawables.vertexPositions[index]), uvs: Array.from(model.drawables.vertexUvs[index]),
      indices: Array.from(model.drawables.indices[index]),
    })),
    parameterChecks,
    presentationRigChecks,
    mouthShapeChecks,
    mouthTransitionChecks,
    neutralBounds: { left: Math.min(...xs), right: Math.max(...xs), bottom: Math.min(...ys), top: Math.max(...ys) },
  };
  const reportFile = path.resolve(process.argv[4] || file.replace(/\.moc3$/, '.core-validation.json'));
  fs.mkdirSync(path.dirname(reportFile), { recursive: true });
  fs.writeFileSync(reportFile, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  model.release();
  moc._release();
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
