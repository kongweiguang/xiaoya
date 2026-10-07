const fs = require("node:fs");
const zlib = require("node:zlib");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

/** 只解码原始 PNG 以读取可见轮廓，不重绘或写回任何位图。 */
function readPng(file) {
  const bytes = fs.readFileSync(file);
  const width = bytes.readUInt32BE(16),
    height = bytes.readUInt32BE(20);
  if (bytes[24] !== 8 || bytes[25] !== 6 || bytes[28] !== 0)
    throw new Error("Expected RGBA8 PNG");
  const parts = [];
  for (let offset = 8; offset < bytes.length; ) {
    const size = bytes.readUInt32BE(offset),
      type = bytes.subarray(offset + 4, offset + 8).toString();
    if (type === "IDAT")
      parts.push(bytes.subarray(offset + 8, offset + 8 + size));
    offset += size + 12;
  }
  const scan = zlib.inflateSync(Buffer.concat(parts)),
    pixels = Buffer.alloc(width * height * 4);
  const stride = width * 4;
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
        const p = a + b - c,
          pa = Math.abs(p - a),
          pb = Math.abs(p - b),
          pc = Math.abs(p - c);
        predict = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (filter !== 0) throw new Error("Invalid PNG filter");
      pixels[y * stride + x] = (scan[y * (stride + 1) + 1 + x] + predict) & 255;
    }
  }
  return { width, height, pixels };
}

/** 用 Core 实际三角形和 UV 采样纹理，避免透明外框、PSD位置或近似矩形误导接缝验收。 */
function geometry(model, id) {
  const index = model.drawables.ids.indexOf(id);
  const vertices = model.drawables.vertexPositions[index],
    uvs = model.drawables.vertexUvs[index];
  const points = [];
  for (let vertex = 0; vertex < vertices.length; vertex += 2) {
    points.push({
      x:
        vertices[vertex] * model.canvasinfo.PixelsPerUnit +
        model.canvasinfo.CanvasOriginX,
      y:
        model.canvasinfo.CanvasOriginY -
        vertices[vertex + 1] * model.canvasinfo.PixelsPerUnit,
      u: uvs[vertex],
      v: uvs[vertex + 1],
    });
  }
  return { points, indices: model.drawables.indices[index] };
}

/** 双三角形插值和原始 alpha 共同决定可见范围，不把画布背景计入身体接触。 */
function alphaAt(mesh, atlas, x, y) {
  for (let triangle = 0; triangle < mesh.indices.length; triangle += 3) {
    const a = mesh.points[mesh.indices[triangle]],
      b = mesh.points[mesh.indices[triangle + 1]],
      c = mesh.points[mesh.indices[triangle + 2]];
    const denominator = (b.y - c.y) * (a.x - c.x) + (c.x - b.x) * (a.y - c.y);
    const wa =
      ((b.y - c.y) * (x - c.x) + (c.x - b.x) * (y - c.y)) / denominator;
    const wb =
      ((c.y - a.y) * (x - c.x) + (a.x - c.x) * (y - c.y)) / denominator;
    const wc = 1 - wa - wb;
    if (wa < 0 || wb < 0 || wc < 0) continue;
    const tx = Math.max(
      0,
      Math.min(
        atlas.width - 1,
        Math.floor((wa * a.u + wb * b.u + wc * c.u) * atlas.width),
      ),
    );
    const ty = Math.max(
      0,
      Math.min(
        atlas.height - 1,
        Math.floor((1 - wa * a.v - wb * b.v - wc * c.v) * atlas.height),
      ),
    );
    return atlas.pixels[(ty * atlas.width + tx) * 4 + 3];
  }
  return 0;
}

/** 用不透明纹理的重叠面积检查关节，透明框相交不会掩盖浮空零件。 */
function overlap(a, b, atlas, maxY = Infinity) {
  const xs = a.points.map((p) => p.x),
    ys = a.points.map((p) => p.y),
    tx = b.points.map((p) => p.x),
    ty = b.points.map((p) => p.y);
  const left = Math.max(Math.min(...xs), Math.min(...tx)),
    right = Math.min(Math.max(...xs), Math.max(...tx));
  const top = Math.max(Math.min(...ys), Math.min(...ty)),
    bottom = Math.min(maxY, Math.max(...ys), Math.max(...ty));
  let area = 0;
  for (let y = top; y < bottom; y += 3)
    for (let x = left; x < right; x += 3)
      if (alphaAt(a, atlas, x, y) >= 128 && alphaAt(b, atlas, x, y) >= 128)
        area += 9;
  return area;
}
/** 每个姿态从默认值开始，避免把前一组合的参数残留当作当前结果。 */
function pose(model, values) {
  model.parameters.values.set(model.parameters.defaultValues);
  for (const [id, value] of Object.entries(values))
    model.parameters.values[model.parameters.ids.indexOf(id)] = value;
  model.update();
}
/** 轮廓接触与绘制遮挡共同证明连接；仅有不透明重叠会漏掉耳座盖在头壳前的视觉错误。 */
async function main() {
  const root = path.resolve(__dirname, "../../../.."),
    // 同时接受仓库相对路径和 Windows 绝对路径，验收输出可以安全放在项目外。
    directory = path.resolve(
      root,
      process.argv[2] || "web/public/avatar/xiaoya",
    ),
    output = path.resolve(
      root,
      process.argv[3] || "assets/avatar/xiaoya/evidence",
    );
  fs.mkdirSync(output, { recursive: true });
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
  const bytes = fs.readFileSync(path.join(directory, "xiaoya.moc3"));
  const moc = context.Live2DCubismCore.Moc.fromArrayBuffer(
      bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length),
    ),
    model = context.Live2DCubismCore.Model.fromMoc(moc);
  const atlas = readPng(path.join(directory, "textures/texture_00.png"));
  const poses = [
    {},
    { ParamAngleY: 1.5, ParamAngleZ: -2, ParamBreath: 0.5 },
    { ParamAngleX: 6, ParamAngleY: 3, ParamAngleZ: 4, ParamBreath: 0.5 },
    { ParamMouthOpenY: 0.8, ParamArmL: 0.7, ParamLeafSwing: 0.5 },
  ];
  for (const x of [-30, 30])
    for (const y of [-30, 30])
      for (const z of [-30, 30])
        for (const body of [-30, 30])
          for (const leaf of [-1, 1])
            for (const arm of [-1, 1])
              for (const breath of [0, 1])
                poses.push({
                  ParamAngleX: x,
                  ParamAngleY: y,
                  ParamAngleZ: z,
                  ParamBodyAngleX: body,
                  ParamLeafSwing: leaf,
                  ParamArmL: arm,
                  ParamArmR: -arm,
                  ParamBreath: breath,
                });
  const checks = [],
    snapshots = [];
  for (let p = 0; p < poses.length; p++) {
    pose(model, poses[p]);
    const meshes = Object.fromEntries(
      model.drawables.ids.map((id) => [id, geometry(model, id)]),
    );
    const areas = {
      neck: overlap(meshes.Head, meshes.Torso, atlas),
      shoulderL: overlap(meshes.ArmL, meshes.Torso, atlas, 940),
      shoulderR: overlap(meshes.ArmR, meshes.Torso, atlas, 940),
      mountL: overlap(meshes.LeafL, meshes.Head, atlas),
      mountR: overlap(meshes.LeafR, meshes.Head, atlas),
    };
    const order = Object.fromEntries(
      model.drawables.ids.map((id, i) => [id, model.drawables.renderOrders[i]]),
    );
    const earMountsBehindHead =
      order.LeafL < order.Head && order.LeafR < order.Head;
    checks.push({
      values: poses[p],
      opaqueOverlapSourcePixels: areas,
      earMountsBehindHead,
      attachmentRenderOrders: {
        LeafL: order.LeafL,
        LeafR: order.LeafR,
        Head: order.Head,
      },
    });
    if (p < 4 || [4, 19, 51, 83, 115, 131].includes(p))
      snapshots.push({
        values: poses[p],
        drawables: model.drawables.ids.map((id, i) => ({
          id,
          ...meshes[id],
          indices: Array.from(meshes[id].indices),
          order: model.drawables.renderOrders[i],
        })),
      });
  }
  pose(model, { ParamLeafSwing: -1 });
  const before = ["LeafL", "LeafR"].map((id) =>
    geometry(model, id).points.slice(6),
  );
  pose(model, { ParamLeafSwing: 1 });
  let mountDelta = 0;
  for (let i = 0; i < 2; i++)
    geometry(model, ["LeafL", "LeafR"][i])
      .points.slice(6)
      .forEach((p, j) => {
        mountDelta = Math.max(
          mountDelta,
          Math.abs(p.x - before[i][j].x),
          Math.abs(p.y - before[i][j].y),
        );
      });
  const minimum = Object.fromEntries(
    Object.keys(checks[0].opaqueOverlapSourcePixels).map((id) => [
      id,
      Math.min(...checks.map((p) => p.opaqueOverlapSourcePixels[id])),
    ]),
  );
  const passed =
    Object.values(minimum).every((area) => area > 100) &&
    mountDelta < 0.01 &&
    checks.every((check) => check.earMountsBehindHead);
  fs.writeFileSync(
    path.join(output, "connection-report.json"),
    JSON.stringify(
      {
        mocSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
        poses: checks,
        poseCount: poses.length,
        minimumOverlap: minimum,
        mountSwingDisplacementSourcePixels: mountDelta,
        earMountsBehindHead: checks.every((check) => check.earMountsBehindHead),
        passed,
      },
      null,
      2,
    ),
  );
  fs.writeFileSync(
    path.join(output, "pose-geometry.json"),
    JSON.stringify(snapshots),
  );
  console.log(
    JSON.stringify({
      poseCount: poses.length,
      minimumOverlap: minimum,
      mountDelta,
      passed,
    }),
  );
  model.release();
  moc._release();
  if (!passed) throw new Error("关节断开、耳座摆动或耳座绘制在头壳前方");
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
