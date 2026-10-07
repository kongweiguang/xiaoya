# 小芽模型制作源与验证

2026-10-07 语义表情／动作升级正在候选阶段：新表现运行代码与六份兼容表情配置已接入，但真实笑眼、眉角及扩大手臂幅度的 19 参数模型尚未通过本轮官方 Editor 门，正式 MOC/CMO/PSD 仍保持下述 16 参数版本。详见 [本轮候选与人工 Editor 接力](PRESENTATION.md)。下文模型几何与 Editor 证据属于此前正式模型，不是本轮新功能验收。

交付为本项目制作的奶白、薄荷绿机器人。正式模型已经在官方 Cubism Editor 5.3.04 中打开、另存、关闭重开并导出，运行于官方 Cubism Core 5.0.0。原画、PSD、可编辑工程、运行模型和同款静态后备图均保留。

## 当前模型（2026-10-07）

正式 MOC SHA256：`e6684a201f6b5038779b498680fdae08038430f21f0235628cf9651142096cda`。

本次修复耳座盖在头壳前方的问题：`LeafL / LeafR` 绘制在 `Head` 之前，头壳遮住耳根，侧边保留绿色耳廓。图层坐标及转头、摆叶、嘴型绑定不变；PSD、官方 CMO、官方 SDK 5.0 MOC、纹理与静态后备图已同步。证据见 [耳座修复](../../../deployment/evidence/live2d-2026-10-07/ears/)。

| 内容 | 已验证结果 |
| --- | --- |
| 制作工程 | 16 个 ArtMesh、16 个参数、6 个旋转变形器、645 个几何关键形态、17 个内嵌图片；官方另存与关闭重开正常 |
| 官方运行导出 | SDK 5.0 / Cubism 5.0 格式、2048 纹理、1280 pixelsPerUnit、防溢色；官方 Core 一致性通过 |
| 头颈 | 头壳绘制在躯干之后，遮住颈圈上沿；呼吸锁住躯干顶部，转头枢轴位于颈根 |
| 手臂 | 双臂向内、向下嵌入肩部，并绘制在躯干后方；枢轴位于被遮住的肩根 |
| 耳座和头饰 | 两侧耳座绘制在头壳后方，由头壳遮住安装根部；耳座和头壳共享转头位移，芽叶上部用 10 顶点连续网格弯曲，底座不独立旋转 |
| 动作连接和遮挡 | 官方 Core 实际顶点与纹理 alpha 检查 132 种姿态，颈、双肩及双耳座均保持不透明轮廓重叠，且耳座实际 renderOrder 始终小于头壳；摆叶时底座位移为零 |
| 工程与运行一致性 | 独立制作模型与官方导出逐一比较 93 种姿态，最大源像素差异为零 |
| 嘴型 | 外唇 68 顶点；开合、圆唇、横展与闭唇连续变化，24 种参数组合有限且无翻折；静音时内嘴退化为线 |
| 原画精修 | 程序清除 2,718 个高饱和疑似杂色和 28,905 个边界／背景非透明像素，内部画法像素改动为零；16 零件重新精修与正式 PNG 逐字节一致 |
| 运行配置 | 本轮补充为 15 个文件引用、6 个兼容表情；2 个历史动作与物理不覆盖嘴部，MotionSync 映射静音及 A/I/U/E/O；新 runtime 由唯一合成器执行短动作，不并行播放历史 motion |

接合检查使用实际纹理 alpha，不能用透明矩形相交代替。最小重叠面积分别为：颈 3,537、左肩 5,445、右肩 4,725、左头饰 20,160、右头饰 17,109 源像素²。数值用于检测脱离，观感另通过官方 Editor、应用和多姿态预览检查。

本版已完成官方工程和导出、132 姿态连接及遮挡、93 姿态几何一致性、网页浅深主题显示和资源指纹检查。此前口型录像及长期私有房间记录见 [应用验收](../../../docs/live2d/acceptance.md)，对应修复前模型；本次几何和口型运行代码未变，没有重新执行长期测量。

## 文件索引

| 内容 | 文件 |
| --- | --- |
| 设计参考与原画 | `concept.png`、精修前 `source-sheet.original.png`、精修后 `source-sheet.png`；零件图不是已拼装角色 |
| 分层与定位 | [xiaoya-layers.psd](xiaoya-layers.psd)、[layers.tsv](layers/layers.tsv)、`layers/*.png`；PSD 16 层、759,711 个非透明像素 |
| 官方可编辑工程 | [xiaoya.cmo3](xiaoya.cmo3)，SHA256 `7f4d338cd8f8d88ebaf61e400c31f5c245f8f1371fedc6dcd98a8ebe6670b03a` |
| 运行模型 | [xiaoya.model3.json](../../../web/public/avatar/xiaoya/xiaoya.model3.json) 引用 MOC、纹理、表情、动作、物理和 MotionSync |
| 实际纹理与静态后备 | [texture_00.png](../../../web/public/avatar/xiaoya/textures/texture_00.png)、[poster.png](../../../web/public/avatar/xiaoya/poster.png) |
| 验证与指纹 | [evidence](evidence/)、[asset-sha256.json](evidence/asset-sha256.json) |
| SDK 与许可 | [SDK 来源和许可](../../../docs/live2d/sdk.md)、[随附许可](../../../web/public/avatar/vendor/) |

## 制作与重建

绑定由独立开源制作工具 [Umamo v0.4.0](https://github.com/umamoorg/umamo/releases/tag/v0.4.0) 建立，再在官方 Editor 中完成保存与 SDK 导出。没有使用第三方样例人物或图片轮播。嘴部由运行时音频独占；结构调整发生在图层定位、绘制顺序和绑定中。

要求 JDK 21+、Node.js；在仓库根目录使用 PowerShell 7。Umamo JAR 位于被忽略的 `.tools/model-export/`，不进入 Python/pnpm 运行依赖。

```powershell
New-Item -ItemType Directory -Force .tools/model-export | Out-Null
Invoke-WebRequest 'https://github.com/umamoorg/umamo/releases/download/v0.4.0/umamo-windows-x64-0.4.0.jar' -OutFile .tools/model-export/umamo-0.4.0.jar
$xiaoyaToolHash = (Get-FileHash .tools/model-export/umamo-0.4.0.jar -Algorithm SHA256).Hash
if ($xiaoyaToolHash -ne '0B5734A2CFAC664898B5C1D8B56EBCC0E6C9C9733D9EFEA7D019D99CEB50F6C8') { throw 'Umamo checksum mismatch' }
javac -encoding UTF-8 -d .tools/model-export assets/avatar/xiaoya/tooling/ExportXiaoya.java assets/avatar/xiaoya/tooling/ValidateSource.java
java -Xmx1g '-Djava.awt.headless=true' -cp '.tools/model-export/umamo-0.4.0.jar;.tools/model-export' ExportXiaoya .tools/model-export/rebuilt assets/avatar/xiaoya/layers/layers.tsv
node assets/avatar/xiaoya/tooling/validate-core.cjs .tools/model-export/rebuilt/xiaoya.moc3
java -Xmx1g '-Djava.awt.headless=true' -cp '.tools/model-export/umamo-0.4.0.jar;.tools/model-export' ValidateSource .tools/model-export/rebuilt
```

重建使用正式精修零件和定位表，不重新提取旧位置。若需复现精修，在带 Pillow 的制作 Python 环境运行 `tooling/refine_layers.py assets/avatar/xiaoya <独立候选目录>`；脚本使用原始零件图与 `layers/extraction.tsv`，不修改内部像素或正式输入。精修后 PNG 的实际缩放由 `ExportXiaoya` 按定位表执行。

重建输出为独立候选，不自动覆盖正式文件。独立导出的兼容 MOC 为 Cubism 3.0，仅用于制作几何复核；正式运行 MOC 由官方 Editor 导出为 Cubism 5.0。CMO 包含创建时间，官方保存还会规范化参数分组，因此重建 CMO 与最终官方工程的字节指纹不同。应比较实际参数几何，并完成以下官方流程后替换：

1. Editor 打开候选；首次载入 Umamo 工程时可能提示版本差异并修复参数分组。另存、关闭并重开，确认工程正常。
2. 在不包含临时网格编辑的已保存工程中导出 SDK 5.0，纹理 2048，pixelsPerUnit 1280，启用防溢色。
3. 校验官方 Core、实际连接和候选／导出几何一致性。保留正式 `model3.json` 中中文参数名、表情、动作、物理及 MotionSync 引用。
4. 同步 MOC、纹理、PSD、CMO 和后备图，更新指纹并重新执行应用口型与房间验收。

前次结构修正的首次导出出现左头饰偏移；丢弃 Editor 未保存编辑、重开已保存工程后重导出恢复零差异。异常输出和诊断保存在 [前次结构证据](../../../deployment/evidence/live2d-2026-10-06/anatomy-final/)，未作为正式运行资源。本次耳座导出与候选几何零差异。参数预览也会使 Editor 显示修改标记，不能据此直接覆盖冻结工程。

复查正式包：

```powershell
node assets/avatar/xiaoya/tooling/validate-core.cjs web/public/avatar/xiaoya/xiaoya.moc3 web/public/avatar/vendor/live2dcubismcore.min.js assets/avatar/xiaoya/evidence/core-validation.json
java -Xmx1g '-Djava.awt.headless=true' -cp '.tools/model-export/umamo-0.4.0.jar;.tools/model-export' ValidateSource assets/avatar/xiaoya assets/avatar/xiaoya/evidence/source-validation.json
node assets/avatar/xiaoya/tooling/validate-package.cjs
node assets/avatar/xiaoya/tooling/validate-connections.cjs
node assets/avatar/xiaoya/tooling/validate-equivalence.cjs .tools/model-export/rebuilt/xiaoya.moc3 web/public/avatar/xiaoya/xiaoya.moc3 assets/avatar/xiaoya/evidence/official-geometry-equivalence.json
```

## 来源和使用边界

原画通过本项目此次角色设计生成，原始文件保留；程序精修已获用户明确授权。Umamo 是 GPL-3.0 制作工具，固定发行 JAR SHA256 为 `0b5734a2cfac664898b5c1d8b56ebcc0e6c9c9733d9efea7d019d99ceb50f6c8`，JAR 与 Umamo 代码不随网页发布。

官方 Editor 5.3.04 安装于当前用户 Programs，2026-10-07 耳座修复时显示试用版剩余 42 天；未购买长期 Editor 许可。SDK 发行许可与 Editor 试用是不同事项，按 [SDK 记录](../../../docs/live2d/sdk.md) 的随附许可使用。本次交付为本地应用与私有房间，未公网发布。用户取消实体手机验收；真人麦克风、扬声器及物理输出延迟未实测，浏览器合成音频结果不能替代它们。
