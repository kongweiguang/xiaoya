const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const crypto = require('node:crypto');

/** 文件指纹直接约束原画来源，避免又用视觉相近的重绘纹理替代 concept。 */
function hash(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

/** 用官方 Core 验证真正的网格、参数和极端组合；单张截图不能证明闭眼与静音仍可用。 */
async function main() {
  const repository = path.resolve(__dirname, '../../../..');
  const directory = path.resolve(process.argv[2]);
  const coreFile = path.join(repository, 'web/public/avatar/vendor/live2dcubismcore.min.js');
  const context = { require, __dirname: path.dirname(coreFile), __filename: coreFile, process,
    Buffer, console, WebAssembly, setTimeout, clearTimeout, TextDecoder, TextEncoder,
    ArrayBuffer, Uint8Array, Float32Array, Int32Array, Uint32Array, Int16Array, Uint16Array };
  context.global = context;
  vm.runInNewContext(fs.readFileSync(coreFile, 'utf8'), context, { filename: coreFile });
  await new Promise(resolve => setTimeout(resolve, 50));
  const core = context.Live2DCubismCore;
  const file = path.join(directory, 'xiaoya.moc3');
  const data = fs.readFileSync(file);
  const bytes = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
  const moc = core.Moc.fromArrayBuffer(bytes);
  if (!moc || moc.hasMocConsistency(bytes) !== 1) throw new Error('Core consistency rejected');
  const model = core.Model.fromMoc(moc);
  try {
    if (model.parameters.count !== 19 || model.drawables.count !== 1)
      throw new Error('Unexpected concept rig contract');
    if (hash(path.join(directory, 'texture.png')) !== hash(path.join(repository, 'assets/avatar/xiaoya/concept.png')))
      throw new Error('Texture differs from original concept bytes');
    const parameterChecks = {};
    for (let parameter = 0; parameter < model.parameters.count; parameter++) {
      model.parameters.values.set(model.parameters.defaultValues);
      if (model.parameters.ids[parameter] === 'ParamMouthForm')
        model.parameters.values[model.parameters.ids.indexOf('ParamMouthOpenY')] = 1;
      model.update();
      const baseline = Array.from(model.drawables.vertexPositions[0]);
      const min = model.parameters.minimumValues[parameter], max = model.parameters.maximumValues[parameter];
      model.parameters.values[parameter] = model.parameters.defaultValues[parameter] === max ? min : max;
      model.update();
      const changed = baseline.some((value, index) => Math.abs(value - model.drawables.vertexPositions[0][index]) > 0.00001);
      if (!changed) throw new Error('Unbound parameter: ' + model.parameters.ids[parameter]);
      parameterChecks[model.parameters.ids[parameter]] = { min, max, default: model.parameters.defaultValues[parameter], movesVertices: changed };
    }
    model.parameters.values.set(model.parameters.defaultValues);
    model.parameters.values[model.parameters.ids.indexOf('ParamMouthOpenY')] = 1;
    model.update();
    const reference = Array.from(model.drawables.vertexPositions[0]);
    let referenceError = 0;
    for (let index = 0; index < reference.length; index += 2) {
      const point = index / 2, x = (point % 81) * 16, y = Math.floor(point / 81) * 16;
      referenceError = Math.max(referenceError, Math.abs(reference[index] - (x - 640) / 640), Math.abs(reference[index + 1] - (640 - y) / 640));
    }
    if (referenceError > 0.00001) throw new Error('Open reference pose distorts the concept: ' + referenceError);
    const indices = model.drawables.indices[0];
    const baselineSigns = [];
    for (let index = 0; index < indices.length; index += 3) baselineSigns.push(Math.sign(area(reference, indices, index)));
    let seed = 0x20261007, minimumArea = Infinity;
    const poses = 600;
    for (let pose = 0; pose < poses; pose++) {
      for (let parameter = 0; parameter < model.parameters.count; parameter++) {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        const fraction = pose < 8 ? ((pose >> (parameter % 3)) & 1) : seed / 0xffffffff;
        model.parameters.values[parameter] = model.parameters.minimumValues[parameter]
          + fraction * (model.parameters.maximumValues[parameter] - model.parameters.minimumValues[parameter]);
      }
      model.update();
      const vertices = model.drawables.vertexPositions[0];
      if (Array.from(vertices).some(value => !Number.isFinite(value))) throw new Error('Nonfinite pose ' + pose);
      for (let index = 0; index < indices.length; index += 3) {
        const signed = area(vertices, indices, index) * baselineSigns[index / 3];
        if (signed < -1e-8) throw new Error('Folded triangle at pose ' + pose + ' / triangle ' + index / 3);
        minimumArea = Math.min(minimumArea, signed);
      }
    }
    const report = { consistency: true, sha256: hash(file), textureSha256: hash(path.join(directory, 'texture.png')),
      textureMatchesConcept: true, sourcePoseMaximumError: referenceError, parameterCount: 19,
      drawableCount: 1, vertices: reference.length / 2, checkedPoses: poses, minimumTriangleArea: minimumArea,
      parameterChecks, officialEditorVerificationIncluded: false };
    const destination = process.argv[3];
    if (destination) fs.writeFileSync(destination, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ consistency: true, textureMatchesConcept: true, referenceError,
      checkedPoses: poses, minimumArea, parameters: 19 }));
  } finally { model.release(); moc._release(); }
}

/** 用实际索引计算绕序，检查包括透明边缘在内的全网格，而不是仅凭包围框判断。 */
function area(vertices, indices, index) {
  const a = indices[index] * 2, b = indices[index + 1] * 2, c = indices[index + 2] * 2;
  return (vertices[b] - vertices[a]) * (vertices[c + 1] - vertices[a + 1])
    - (vertices[c] - vertices[a]) * (vertices[b + 1] - vertices[a + 1]);
}

/** 输出简洁诊断，避免错误栈附带内嵌 Core 二进制的大段内容。 */
main().catch(error => { console.error(error.message); process.exitCode = 1; });
