import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { validateModel } from './validate-model.mjs';

const directory = path.resolve(import.meta.dirname, '../../../../web/public/avatar/xiaoya');
const poster = 'concept-v1/poster.png';

/** 只复制当前资产到测试专属目录；破坏性样例绝不修改浏览器正在使用的运行包。 */
function isolatedPackage(context) {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xiaoya-poster-test-'));
  /** 测试退出也回收副本，但路径必须仍是本次创建的临时目录，不触及原包或宽目录。 */
  const cleanup = () => {
    const resolved = path.resolve(fixture);
    assert.ok(resolved.startsWith(path.resolve(os.tmpdir()) + path.sep));
    assert.ok(path.basename(resolved).startsWith('xiaoya-poster-test-'));
    assert.equal(fs.lstatSync(resolved).isSymbolicLink(), false);
    fs.rmSync(resolved, { recursive: true });
  };
  context.after(cleanup);
  const model = path.join(fixture, 'model');
  fs.cpSync(directory, model, { recursive: true });
  return model;
}

/** 当前海报与真实 Core 一起留证，未使用的根海报不能替代浏览器的实际后备图。 */
test('当前资产实时验证 19 参数和 35 种表现组合', async () => {
  const report = await validateModel(directory);
  assert.equal(report.parameterCount, 19);
  assert.equal(report.styleGestureCombinations, 35);
  assert.equal(report.combinationFrames, 35 * 181);
  assert.equal(report.mouthParametersExclusive, true);
  assert.equal(report.officialEditorVerificationIncluded, false);
  assert.equal(report.ordinaryParameterCount + report.blendShapeCount, 19);
  assert.equal(report.poster, poster);
  assert.deepEqual(report.assets.find(asset => asset.file === poster), {
    file: poster,
    sha256: createHash('sha256').update(fs.readFileSync(path.join(directory, poster))).digest('hex'),
  });
  assert.equal(report.assets.some(asset => asset.file === 'poster.png'), false);
  for (const parameter of Object.values(report.parameterChecks))
    assert.ok(parameter.displacementPixels >= 0.01);
});

/** 实际后备图损坏必须失败，即使同包中保留着完整但已不用的根海报也不能补位。 */
test('实际后备海报损坏不能被旧根海报掩盖', async context => {
  const model = isolatedPackage(context);
  fs.writeFileSync(path.join(model, poster), 'not-a-png');
  await assert.rejects(validateModel(model), /Expected a transparent RGBA PNG/);
});

/** 当前包约定的文件缺失就是包不完整，不猜目录、搜索其他图片或静默回退。 */
test('实际后备海报缺失必须失败', async context => {
  const model = isolatedPackage(context);
  fs.unlinkSync(path.join(model, poster));
  await assert.rejects(validateModel(model), /ENOENT/);
});

/** 无消费者的历史图片不属于现行资源闭包，损坏它不能阻碍当前包的真实验证。 */
test('未使用的根海报不参与当前包验收', async context => {
  const model = isolatedPackage(context);
  fs.writeFileSync(path.join(model, 'poster.png'), 'not-a-png');
  const report = await validateModel(model);
  assert.equal(report.poster, poster);
  assert.equal(report.assets.some(asset => asset.file === 'poster.png'), false);
});

/** 候选海报由调用者明确选择，同一检查器不借 MOC 所在目录推断包版本或后备图片。 */
test('候选包可显式指定自己的包内海报', async context => {
  const model = isolatedPackage(context);
  fs.copyFileSync(path.join(model, poster), path.join(model, 'candidate-poster.png'));
  fs.writeFileSync(path.join(model, poster), 'not-a-png');
  fs.writeFileSync(path.join(model, 'poster.png'), 'not-a-png');
  const report = await validateModel(model, { poster: 'candidate-poster.png' });
  assert.equal(report.poster, 'candidate-poster.png');
  assert.ok(report.assets.some(asset => asset.file === 'candidate-poster.png'));
  assert.equal(report.assets.some(asset => asset.file === poster || asset.file === 'poster.png'), false);
});

/** CLI 必须在进入 Core 前拒绝缺少值的显式选项，不能忽略拼错命令后报告默认包成功。 */
test('显式海报选项必须提供路径', () => {
  const entry = path.join(import.meta.dirname, 'validate-model.mjs');
  for (const args of [['--poster'], ['--poster', '--stable']]) {
    const result = spawnSync(process.execPath, [entry, ...args], { encoding: 'utf8' });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /必须指定海报文件/);
  }
});

/** stable 门以实际参数类型决定结果，不能把旧 BlendShape 包当成新普通参数工程。 */
test('稳定版门独立于普通 Core 可加载检查', async () => {
  const bytes = fs.readFileSync(path.join(directory, 'xiaoya.model3.json'));
  const manifest = JSON.parse(bytes);
  // 当前 manifest 指向历史 concept-v1；候选晋升后此门必须改为严格成功验收。
  if (manifest.FileReferences.Moc.startsWith('concept-v1/'))
    await assert.rejects(validateModel(directory, { stable: true }), /不得含 BlendShape/);
  else {
    const report = await validateModel(directory, { stable: true });
    assert.equal(report.ordinaryParameterCount, 19);
    assert.equal(report.blendShapeCount, 0);
  }
});

/** 恶意 manifest 在进入官方二进制前就失败；临时输入不允许读包外文件。 */
test('资源路径不能逃出模型包', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xiaoya-model-test-'));
  const model = path.join(fixture, 'model');
  try {
    fs.mkdirSync(model);
    fs.writeFileSync(path.join(fixture, 'outside.moc3'), 'not-a-model');
    fs.writeFileSync(path.join(model, 'xiaoya.model3.json'), JSON.stringify({
      FileReferences: { Moc: '../outside.moc3' },
    }));
    await assert.rejects(validateModel(model), /超出资产包/);
  } finally {
    const resolved = path.resolve(fixture);
    assert.ok(resolved.startsWith(path.resolve(os.tmpdir()) + path.sep));
    assert.ok(path.basename(resolved).startsWith('xiaoya-model-test-'));
    fs.rmSync(resolved, { recursive: true });
  }
});
