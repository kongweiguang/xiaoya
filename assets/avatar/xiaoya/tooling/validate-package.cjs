const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const repository = path.resolve(__dirname, '../../../..');
const modelDirectory = path.resolve(argument('--model-dir', path.join(repository, 'web/public/avatar/xiaoya')));
const checkedFiles = new Map();
let parameterChecks;
const mouthParameters = new Set(['ParamMouthOpenY', 'ParamMouthForm']);

/** 验收 staging 包时显式指定目录和证据，保持默认命令继续检查正式包。 */
function argument(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index < 0) return fallback;
  const value = process.argv[index + 1];
  if (!value || value.startsWith('--')) throw new Error('Missing argument value: ' + name);
  return value;
}

/** 仅接受模型目录内的同源资源，避免引用存在却把部署包外文件遗漏在交付检查之外。 */
function readAsset(relative) {
  if (typeof relative !== 'string' || relative.length === 0) {
    throw new Error('Missing model asset reference');
  }
  const resolved = path.resolve(modelDirectory, relative);
  if (!resolved.startsWith(modelDirectory + path.sep)) {
    throw new Error('Asset reference escapes the model directory: ' + relative);
  }
  const bytes = fs.readFileSync(resolved);
  checkedFiles.set(relative, crypto.createHash('sha256').update(bytes).digest('hex'));
  return bytes;
}

/** 每项配置使用实际 MOC 参数定义，防止 JSON 拼写正确但运行时目标不存在。 */
function checkParameter(id, allowMouth = true) {
  const limits = parameterChecks[id];
  if (!limits || (!allowMouth && mouthParameters.has(id))) {
    throw new Error('Invalid or forbidden parameter target: ' + id);
  }
  return limits;
}

/** 对照官方 motion3 编码统计曲线，兼顾今后增加贝塞尔和阶跃片段的可复核性。 */
function checkMotionCurve(curve, duration) {
  if (curve.Target !== 'Parameter') throw new Error('Unsupported motion target');
  const limits = checkParameter(curve.Id, false);
  const data = curve.Segments;
  let segments = 0;
  let points = 1;
  let offset = 2;
  let previousTime = data[0];
  if (data.length < 2 || previousTime !== 0) throw new Error('Invalid motion start');
  if (!Number.isFinite(data[1]) || data[1] < limits.min || data[1] > limits.max) {
    throw new Error('Initial motion value exceeds its MOC parameter range');
  }
  while (offset < data.length) {
    const kind = data[offset++];
    if (![0, 1, 2, 3].includes(kind)) throw new Error('Invalid motion segment kind');
    const pointCount = kind === 1 ? 3 : 1;
    for (let point = 0; point < pointCount; point++) {
      const time = data[offset++];
      const value = data[offset++];
      if (!Number.isFinite(time) || time < previousTime || time > duration ||
          !Number.isFinite(value) || value < limits.min || value > limits.max) {
        throw new Error('Invalid time or value in motion ' + curve.Id);
      }
      previousTime = time;
    }
    segments++;
    points += pointCount;
  }
  if (previousTime !== duration) throw new Error('Motion curve omits its end frame');
  return { segments, points };
}

/** 资源引用、参数范围及口型独占一起验收；播放同步和官方 Editor 各自保留独立证据。 */
function main() {
  const core = JSON.parse(fs.readFileSync(argument('--core-report', path.join(repository,
    'assets/avatar/xiaoya/evidence/core-validation.json')), 'utf8'));
  parameterChecks = core.parameterChecks;
  const model = JSON.parse(readAsset('xiaoya.model3.json'));
  if (model.Version !== 3) throw new Error('Expected model3 version 3');
  const references = model.FileReferences;
  const moc = readAsset(references.Moc);
  if (!core.consistency || crypto.createHash('sha256').update(moc).digest('hex') !== core.sha256) {
    throw new Error('Core evidence does not match the current MOC');
  }
  if (references.Textures.length !== 1) throw new Error('Expected one model texture atlas');
  for (const relative of [...references.Textures, 'poster.png']) {
    const png = readAsset(relative);
    if (png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a' || png[25] !== 6) {
      throw new Error('Expected a transparent RGBA PNG: ' + relative);
    }
    if (png.readUInt32BE(16) <= 0 || png.readUInt32BE(20) <= 0) throw new Error('Invalid PNG dimensions');
  }
  const display = JSON.parse(readAsset(references.DisplayInfo));
  const displayIds = new Set();
  for (const parameter of display.Parameters) {
    checkParameter(parameter.Id);
    if (displayIds.has(parameter.Id)) throw new Error('Duplicate display parameter');
    displayIds.add(parameter.Id);
  }
  if (displayIds.size !== core.parameterCount) throw new Error('Display names omit MOC parameters');
  for (const group of model.Groups) {
    if (group.Target !== 'Parameter') throw new Error('Unsupported model group target');
    for (const id of group.Ids) checkParameter(id);
  }
  for (const expression of references.Expressions) {
    const config = JSON.parse(readAsset(expression.File));
    if (config.Type !== 'Live2D Expression') throw new Error('Invalid expression type');
    for (const parameter of config.Parameters) {
      checkParameter(parameter.Id, false);
      if (!Number.isFinite(parameter.Value) || !['Add', 'Multiply', 'Overwrite'].includes(parameter.Blend)) {
        throw new Error('Invalid expression parameter');
      }
    }
  }
  let motionCount = 0;
  for (const group of Object.values(references.Motions)) {
    for (const reference of group) {
      const motion = JSON.parse(readAsset(reference.File));
      if (motion.Version !== 3 || motion.Curves.length !== motion.Meta.CurveCount) {
        throw new Error('Invalid motion metadata');
      }
      let segments = 0;
      let points = 0;
      for (const curve of motion.Curves) {
        const counts = checkMotionCurve(curve, motion.Meta.Duration);
        segments += counts.segments;
        points += counts.points;
      }
      if (segments !== motion.Meta.TotalSegmentCount || points !== motion.Meta.TotalPointCount) {
        throw new Error('Motion counts disagree with the encoded curves');
      }
      motionCount++;
    }
  }
  const physics = JSON.parse(readAsset(references.Physics));
  let inputs = 0;
  let outputs = 0;
  let vertices = 0;
  for (const setting of physics.PhysicsSettings) {
    for (const input of setting.Input) checkParameter(input.Source.Id);
    for (const output of setting.Output) {
      checkParameter(output.Destination.Id, false);
      if (output.VertexIndex < 1 || output.VertexIndex >= setting.Vertices.length) {
        throw new Error('Physics output references an absent particle');
      }
    }
    inputs += setting.Input.length;
    outputs += setting.Output.length;
    vertices += setting.Vertices.length;
  }
  if (physics.Version !== 3 || physics.PhysicsSettings.length !== physics.Meta.PhysicsSettingCount ||
      inputs !== physics.Meta.TotalInputCount || outputs !== physics.Meta.TotalOutputCount ||
      vertices !== physics.Meta.VertexCount) throw new Error('Invalid physics metadata');
  const sync = JSON.parse(readAsset(references.MotionSync));
  if (sync.Version !== 1 || sync.Settings.length !== sync.Meta.SettingCount || sync.Settings.length !== 1) {
    throw new Error('Invalid MotionSync metadata');
  }
  const setting = sync.Settings[0];
  if (setting.AnalysisType !== 'CRI' || setting.UseCase !== 'Mouth') throw new Error('Expected CRI mouth analysis');
  const syncIds = new Set();
  for (const parameter of setting.CubismParameters) {
    const limits = checkParameter(parameter.Id);
    if (!mouthParameters.has(parameter.Id) || parameter.Min !== limits.min || parameter.Max !== limits.max) {
      throw new Error('MotionSync range disagrees with the MOC');
    }
    syncIds.add(parameter.Id);
  }
  if (syncIds.size !== 2) throw new Error('MotionSync must control opening and mouth form');
  const mappings = {};
  for (const mapping of setting.Mappings) {
    mappings[mapping.Id] = mapping;
    for (const target of mapping.Targets) {
      const limits = checkParameter(target.Id);
      if (!syncIds.has(target.Id) || target.Value < limits.min || target.Value > limits.max) {
        throw new Error('MotionSync mapping exceeds its mouth parameter range');
      }
    }
  }
  for (const vowel of ['Silence', 'A', 'I', 'U', 'E', 'O']) {
    if (!mappings[vowel] || mappings[vowel].Targets.length !== 2) throw new Error('Missing vowel mapping');
  }
  for (const target of mappings.Silence.Targets) {
    if (target.Value !== 0) throw new Error('Silence must restore the neutral closed mouth');
  }
  const assets = [];
  for (const file of [...checkedFiles.keys()].sort()) assets.push({ file, sha256: checkedFiles.get(file) });
  const report = {
    checkedAt: new Date().toISOString(),
    mocSha256: core.sha256,
    fileCount: assets.length,
    parameterCount: core.parameterCount,
    expressionCount: references.Expressions.length,
    motionCount,
    physicsSettingCount: physics.PhysicsSettings.length,
    mouthParametersExclusive: true,
    motionSyncVowels: ['Silence', 'A', 'I', 'U', 'E', 'O'],
    runtimeAssetReferencesValid: true,
    officialEditorVerificationIncluded: false,
    assets,
  };
  const output = path.resolve(argument('--output', path.join(repository,
    'assets/avatar/xiaoya/evidence/package-validation.json')));
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({ fileCount: assets.length, expressionCount: report.expressionCount,
    motionCount, mouthParametersExclusive: true, runtimeAssetReferencesValid: true }));
}

main();
