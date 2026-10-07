const fs = require('node:fs');
const path = require('node:path');

/** 只装配独立候选目录；正式二进制的替换必须另过官方 Editor 门，不能借此脚本自动发布。 */
function main() {
  const repository = path.resolve(__dirname, '../../../..');
  const staging = path.join(repository, '.tools/model-export');
  if (!process.argv[2]) throw new Error('用法：node prepare-presentation.cjs <候选目录>');
  const candidate = path.resolve(process.argv[2]);
  if (!candidate.startsWith(staging + path.sep) || !fs.existsSync(path.join(candidate, 'xiaoya.cmo3')))
    throw new Error('目标必须是 .tools/model-export 内已经生成的候选工程');
  const source = path.join(repository, 'web/public/avatar/xiaoya');
  const rig = JSON.parse(fs.readFileSync(path.join(__dirname, '../presentation-rig.json'), 'utf8'));
  const manifest = JSON.parse(fs.readFileSync(path.join(source, 'xiaoya.model3.json'), 'utf8'));
  for (const name of ['xiaoya.physics3.json', 'xiaoya.motionsync3.json'])
    fs.copyFileSync(path.join(source, name), path.join(candidate, name));
  for (const name of ['expressions', 'motions'])
    fs.cpSync(path.join(source, name), path.join(candidate, name), { recursive: true });
  fs.cpSync(path.join(__dirname, '../layers'), path.join(candidate, 'layers'), { recursive: true });
  const display = JSON.parse(fs.readFileSync(path.join(source, 'xiaoya.cdi3.json'), 'utf8'));
  const currentIds = new Set(display.Parameters.map(
    /** 多次准备候选不重复追加参数；正式包未来也可能已经具有这些真实绑定。 */
    (parameter) => parameter.Id
  ));
  for (const parameter of rig.parameters)
    if (!currentIds.has(parameter.Id)) display.Parameters.push(parameter);
  for (const reference of manifest.FileReferences.Expressions) {
    const file = path.join(candidate, reference.File);
    const expression = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const parameter of rig.expressions[reference.Name] ?? []) {
      const previous = expression.Parameters.findIndex(
        /** 按 ID 更新，使重跑幂等而不造成同一表情内 Add/Overwrite 竞争。 */
        (item) => item.Id === parameter.Id
      );
      if (previous >= 0) expression.Parameters[previous] = parameter;
      else expression.Parameters.push(parameter);
    }
    fs.writeFileSync(file, JSON.stringify(expression, null, 2) + '\n');
  }
  fs.writeFileSync(path.join(candidate, 'xiaoya.cdi3.json'), JSON.stringify(display, null, 2) + '\n');
  fs.writeFileSync(path.join(candidate, 'xiaoya.model3.json'), JSON.stringify(manifest, null, 2) + '\n');
  console.log(JSON.stringify({ candidate, parameters: display.Parameters.length, expressions: manifest.FileReferences.Expressions.length, officialEditorVerificationIncluded: false }));
}

main();
