const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const crypto = require("node:crypto");
const root = path.resolve(__dirname, "../../../..");

/** 保留全身与嘴部旧门并比较笑眼/眉角组合，避免官方导出只在中性形态一致却丢失新表情绑定。 */
async function main() {
  if (!process.argv[2] || !process.argv[3] || !process.argv[4]) {
    throw new Error(
      "用法：node validate-equivalence.cjs <候选 MOC> <官方 MOC> <报告 JSON>",
    );
  }
  const coreFile = root + "/web/public/avatar/vendor/live2dcubismcore.min.js";
  const context = {
    require,
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
    __dirname: path.dirname(coreFile),
    __filename: coreFile,
  };
  context.global = context;
  vm.runInNewContext(fs.readFileSync(coreFile, "utf8"), context, {
    filename: coreFile,
  });
  await new Promise((resolve) => setTimeout(resolve, 50));
  const core = context.Live2DCubismCore;
  const sources = [process.argv[2], process.argv[3]].map((file) => {
    const bytes = fs.readFileSync(path.resolve(root, file));
    const moc = core.Moc.fromArrayBuffer(
      bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length),
    );
    if (!moc) throw new Error("Core 无法读取模型：" + file);
    return {
      file,
      sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
      moc,
      model: core.Model.fromMoc(moc),
    };
  });
  const [candidate, exported] = sources.map((source) => source.model);
  if (
    candidate.drawables.count !== exported.drawables.count ||
    candidate.drawables.ids.some(
      (id) => !exported.drawables.ids.includes(id),
    ) ||
    JSON.stringify(candidate.parameters.ids) !==
      JSON.stringify(exported.parameters.ids)
  ) {
    throw new Error("零件或参数集合发生变化，不能仅用位置差异判断工程一致");
  }
  for (const name of ["minimumValues", "maximumValues", "defaultValues"]) {
    if (
      JSON.stringify(Array.from(candidate.parameters[name])) !==
      JSON.stringify(Array.from(exported.parameters[name]))
    ) {
      throw new Error("参数范围发生变化：" + name);
    }
  }
  const poses = [{}];
  for (const id of sources[0].model.parameters.ids) {
    const index = sources[0].model.parameters.ids.indexOf(id);
    for (const value of [
      sources[0].model.parameters.minimumValues[index],
      sources[0].model.parameters.maximumValues[index],
    ])
      poses.push({ [id]: value });
  }
  for (const x of [-30, 0, 30])
    for (const y of [-30, 30])
      for (const z of [-30, 0, 30])
        for (const breath of [0, 1])
          poses.push({
            ParamAngleX: x,
            ParamAngleY: y,
            ParamAngleZ: z,
            ParamBreath: breath,
          });
  for (const form of [-1, 0, 1])
    for (const open of [0, 0.001, 0.01, 0.02, 0.05, 0.25, 0.5, 1])
      poses.push({ ParamMouthForm: form, ParamMouthOpenY: open });
  if (candidate.parameters.ids.includes('ParamEyeSmile'))
    for (const smile of [0.3, 1])
      for (const open of [0, 0.4, 1])
        for (const brow of [-1, 0, 1])
          poses.push({ ParamEyeSmile: smile, ParamEyeLOpen: open, ParamEyeROpen: open,
            ParamBrowLAngle: brow, ParamBrowRAngle: -brow, ParamArmL: 0.85 });
  const comparisons = [];
  for (const pose of poses) {
    for (const { model } of sources) {
      model.parameters.values.set(model.parameters.defaultValues);
      for (const [id, value] of Object.entries(pose))
        model.parameters.values[model.parameters.ids.indexOf(id)] = value;
      model.update();
    }
    const [before, after] = sources.map((source) => source.model);
    let maximumDelta = 0;
    for (let mesh = 0; mesh < before.drawables.count; mesh++) {
      const savedIndex = after.drawables.ids.indexOf(
        before.drawables.ids[mesh],
      );
      const original = before.drawables.vertexPositions[mesh],
        saved = after.drawables.vertexPositions[savedIndex];
      if (original.length !== saved.length) throw new Error("顶点数量发生变化");
      for (let vertex = 0; vertex < original.length; vertex++)
        maximumDelta = Math.max(
          maximumDelta,
          Math.abs(
            original[vertex] * before.canvasinfo.PixelsPerUnit -
              saved[vertex] * after.canvasinfo.PixelsPerUnit,
          ),
        );
    }
    if (!Number.isFinite(maximumDelta) || maximumDelta > 0.01)
      throw new Error(
        "官方导出改变绑定几何：" + JSON.stringify({ pose, maximumDelta }),
      );
    comparisons.push({ pose, maximumSourcePixelDelta: maximumDelta });
  }
  const report = {
    sources: sources.map(({ file, sha256, model }) => ({
      file,
      sha256,
      canvas: model.canvasinfo,
    })),
    poses: comparisons,
    poseCount: comparisons.length,
    maximumSourcePixelDelta: Math.max(
      ...comparisons.map((pose) => pose.maximumSourcePixelDelta),
    ),
    passed: true,
  };
  const output = path.resolve(root, process.argv[4]);
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, JSON.stringify(report, null, 2));
  sources.forEach(({ model, moc }) => {
    model.release();
    moc._release();
  });
  console.log(
    JSON.stringify({
      poseCount: report.poseCount,
      maximumSourcePixelDelta: report.maximumSourcePixelDelta,
      passed: report.passed,
    }),
  );
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
