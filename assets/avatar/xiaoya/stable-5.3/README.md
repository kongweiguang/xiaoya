# 稳定版 Editor 候选：技术验收通过，FREE 保存待验

从保留的 `concept.png` 在官方 Cubism Editor 5.3.04 中重建模型。当前候选为 **19 个有效普通参数、0 个 BlendShape**，保留既定 ID／范围、7 种风格与 5 种手势。

## 当前状态

核对日期 **2026-10-11**。最终官方源为 `xiaoya-native-final19-20261011.cmo3`，保存于 **11:53:16 +08:00**，**2,751,040 字节**，SHA-256 `003f552f4cc9ba1409fa8e95a86acdd7a345315f79935876325da913dc55b9e7`。同字节保护副本为 `xiaoya-native-final19-checkpoint-20261011.cmo3`。工程已官方保存、关闭、重开，复核代表姿态后再次保存、关闭、重开并导出。

| 项目 | 结果 |
| --- | --- |
| 参数清理 | 用户明确确认后，官方“删除空参数”移除指定 8 项；原 27 参数工程与 GUID CSV 保留，未导入删除型 CSV |
| 参数与绑定 | Editor 参数表及实际 Core 均确认 19 普通参数、0 BlendShape；19 项均改变几何或透明度，无剩余未绑定项；ID／范围／默认值与清理前目标项一致 |
| 官方往返与导出 | 最终工程真实往返；SDK 5.0／Cubism 5.0 兼容导出、单张 2048×2048 纹理；重开源再导出的 MOC、贴图、CDI 与原始 model3 共 4 文件逐字节一致 |
| 完整三组静态样机 | 实际生产渲染器生成并逐页复核 334 张图：中性、12 嘴形、15 眼部、9 头身手臂代表、108 眼笑视线组合与189单侧／双侧手臂头身组合 |
| Core 几何诊断 | 2469 组样本，每组 104 个实际三角形有方向参照；翻面、退化与未覆盖均为 0 |
| 动态组合 | 7 风格×5 手势×30／60两目标共70组，436组插值样本通过；逐页复核280阶段／停止截图、35实际峰值图、44动态入口样机图 |
| 实际帧间隔 | 60目标 p50 14.1 ms／p95 21.1 ms；30目标 p50 34.6 ms／p95 35.2 ms；原20%门限未改 |
| 停止与释放 | 每组停止后实际嘴参数归零；销毁后无新增生产帧，待执行RAF为0；浏览器关闭，30／60矩阵控制台无异常 |
| FREE 实际保存 | **未通过**；实际标题仍为既有试用剩余39天；没有修改许可状态 |
| 正式资产晋升 | **未执行**；候选留在暂存区，旧网页运行包保持原身份 |

## 本次原生制作与清理

左右眼白各25顶点；共享笑眼，独立眼开合、视线与瞳孔闭态透明度。半睁瞳孔淡灰已在原生父级通过 EyeOpen=0.25 的100%透明度键形修正；0仍为0%，1仍为100%。BodyFollow保留原头部、双肩、胸部和双腿六根及后代；头X/Y为温和二维轮廓转向，身体X有实际非均匀联动；双手分离原肩段与手掌并调整腹前绘制顺序。

本次明确删除 `ParamEyeRSmile`、`ParamBrowLX`、`ParamBrowRX`、`ParamBrowLForm`、`ParamBrowRForm`、`ParamCheek`、`ParamBodyAngleY`、`ParamBodyAngleZ`。清理前官方另存 `xiaoya-native-pre-cleanup-27-checkpoint-20261011.cmo3`；原27参数预览、各制作检查点及GUID CSV均保留。清理后的目标19项元数据与清理前逐项一致。

最终导出在 `runtime-final19-20261011/`，第二份原始往返导出在 `runtime-final19-roundtrip-20261011/`。首份官方 model3 原字节以 `official-export.*.model3.json` 保留，项目自有8个表情与MotionSync单独接入并记录来源；新MOC与贴图均来自本次官方导出。MOC SHA-256 `601c6d70eab756fd2265973533a3315b0e7fc5440770a0effd2495c300bae0dc`。

像素复核未见闭眼露虹膜、半睁灰瞳、嘴部穿插、白斑、肩腕连接脱落或腹后藏手。闭态笑眼左右弧线仍有轻微曲率差异，记录为外观特征，不称完全对称。此模型QA使用受控v2 cue与嘴形包络，不创建真人音频或Jev调用；真实房间结果另见 [交付验收](../../../../docs/delivery-acceptance.md)。

## 证据与失败保留

有限回执为 `final19-acceptance-20261011.json`，绑定最终工程、两次官方原始导出、当前生产源码、自动检查和独立像素复核。官方GUI截图在 `.tools/cua-evidence/native-final19-20261011/`；静态结果及22张复核页在 `.tools/live2d-verification/preview-20261011/render-2026-10-11T03-59-23-223Z/`；动态原结果、21张复核页与35原图展示页在 `.tools/live2d-verification/candidate-browser-20261009/evidence-2026-10-11T04-08-38-219Z/`。

首次动态运行的60目标间隔为20.8ms；累计截止点修正后的第二次为20.1ms，两次原报告仍为失败。进一步定位到QA与生产各有60截止点，不同相位吞帧；QA60改为透传原生RAF，生产原60调度负责实际限频，QA30保留累计截止点。原生时间戳、生产源码、取消语义及20%验收门限均保持；先以同一生产模型预检，再执行本次完整重跑，不用独立空页面帧率填入模型门限。

FREE门仍开放：[官方说明](https://help.live2d.com/other/other_12/)指出试用不能在期限届满前提前停用。当前试用保存与导出只证明原生技术往返；全部硬门通过后才可整体晋升。

## 历史制作进展（2026-10-10）

以下“未保存”“输入暂停”“18 项有效”等状态为当天历史事实，已被上方 10 月 11 日当前状态取代。

- 通过本会话正式提供的 `mcp__node_repl` 与 `@oai/sky` 操作官方 Editor 5.3.04，真实选窗、单步操作后刷新。此前窗口裁剪故障已恢复，不复用历史坐标或旧 MCP 桥。
- 在原手臂 UV 与肩轴父级内，将左右手掌拆为 `HandL`（ArmL2）／`HandR`（ArmR2），保留原 `ArmL`／`ArmR` 的肩段。手掌绘制顺序为 510；肩段中性和正向为 500，负向关键形为 505。原 `ParamArmL`／`ParamArmR` 的 ID 与范围不变，抬手角度改为约 ±85°，没有新增参数。
- 中性、单侧挥手 0.5／1、单侧收手 -0.5／-1、双侧收手 -1，以及左 -0.25／-0.75 加右 -1 的真实截图位于 `.tools/cua-evidence/arm-front-20261010/`。负向前臂与手掌已连续露在腹前；仍需检查 0 到 -0.25 的层级过渡，以及头身反向组合，不能凭这些局部图替代完整样机。
- 实际树确认 HeadRotation2 的共同后代包含左右眼白、瞳孔视线父、眉、嘴、叶；原有六个根为共同头部父、两肩轴、胸部、两腿。后续 BodyAngleX 需要把六根纳入共同父，保留全部子层级和键形。
- **未保存眼部修改**：右眼白新增 `EyeSmile_R`（Warp7），通过官方参数“变更”把现有 0／1 键形转到共享 `ParamEyeSmile`；原右眼默认微笑参数现在无键形。左右各新增 `PupilVisible_R`（Warp8）／`PupilVisible_L`（Warp9）作为 GazeY 的父级，分别在本侧 EyeOpen=0 时透明度 0、1 时 100；实际双眼闭合快照未见虹膜。左右笑眼弧线仍需对称精修，并核对上下左右视线、中间开合。
- 当前 GUI 输入暂停：原生通道两次明确报告 `user input was detected in this window; call get_window_state before continuing`，其中一次是 Ctrl+S 保存请求。已按 computer-use 保护停止输入，保留打开的未保存工程，等待电脑空闲；没有关闭、重启或终止 Editor。未把尚未成功的保存写成新 SHA。
- 原研究源 `xiaoya-native-layers-study-20261009.cmo3` 于 19:33:17.5736536 保存为 1,374,900 字节，SHA-256 `ec12aa66123ec6dfa8021ce60e7eee3129e82d4d92aa211244b3b39741a3bbe5`；随后官方另存到当前检查点。不同文件名保存后的文件身份分别记录。

## 历史制作记录（2026-10-09，非当前状态）

以下记录只保留此前路线与恢复过程，当前保存身份、许可状态、有效绑定和未完成门限以上方 2026-10-11 记录为准。历史上眼睑带研究文件 `xiaoya-lid-mask-study.cmo3` 未成为当前源或运行包；旧候选的往返与导出不能替代本次验收。

- 通过已安装的官方 `node_repl.exe` MCP 服务及其 `@oai/sky` 恢复真实窗口选择、截图和输入。当前会话使用官方 MCP 客户端，未绕过 `sky`、未关闭 Codex、未改 Windows 安全设置。仅本任务的 REPL 采用与已授权完整访问相符的 `--disable-sandbox`。
- 官方 Editor 实际版本为 **5.3.04**。第四候选在本次重新打开、复现闭眼白羽斑，并官方另存为 `xiaoya-official-rig-20261009.cmo3`；该文件仍是历史路线研究检查点，不能视为完成的新模型。
- 原 `xiaoya-layers.psd` 被官方读取器拒绝：实际日志报告 `error signature :: @ 0x00000026`。只增加标准 `ResolutionInfo` 图像资源后，官方读取器成功解析全部 16 层；第一份候选图层和合成像素区逐字节不变。原 PSD、PNG 和 CMO3 均保留。
- 第二份 `xiaoya-layers-editor-ordered.psd` 只调整图层记录及其原始通道块顺序，所有图层通道字节与坐标保持原样。已在官方 Editor 新建独立分层模型并实际确认眼、眉、嘴可见；没有第三方生成 CMO3，也没有重新绘图或缩放图层。
- 新分层工程 `xiaoya-native-layers-study-20261009.cmo3` 正在原生逐项绑定。当前 **18 项普通参数有实际有效绑定、0 个 BlendShape**：`ParamEyeLOpen`、`ParamEyeROpen` 控制眼白开合并裁剪原瞳孔；`ParamEyeBallX` 控制左右瞳孔水平位移，`ParamEyeBallY` 通过两个独立父变形器控制垂直视线；`ParamMouthOpenY` 控制内嘴及嘴唇开合，并在闭态隐藏内嘴；`ParamMouthForm` 通过共同父变形器收圆／展宽内嘴和嘴唇；`ParamArmL`、`ParamArmR` 使用各自肩部旋转父级完成收臂及约 110° 抬手；`ParamAngleX`、`ParamAngleY` 使用独立头部曲面移动整组头部图层，`ParamAngleZ` 使用颈部旋转父级产生约 ±7.6° 倾斜；`ParamBrowLY`、`ParamBrowRY` 分别控制左右眉高低，`ParamBrowLAngle`、`ParamBrowRAngle` 使用独立旋转父级产生约 ±11° 眉角，可与高低叠加；`ParamBreath` 保持胸部下缘，微调上缘形成呼吸；`ParamLeafSwing` 保持双叶底部，左右移动上缘形成回弹；`ParamEyeSmile` 已在左眼白父曲面形成实际弧度，**右眼尚未绑定**。X/Y 当前是小幅整体位移，尚未完成立体转向精修；手臂在抬起时被头部遮挡，绘制顺序需要精修。已查看开合 0／0.5／1、嘴形 -1／0／1、双臂、头部、眉部、呼吸及双叶端点的真实形变。通过菜单恢复默认值后，MouthOpen=0／MouthForm=0 的闭唇为细线、内嘴隐藏；此前厚白环截图是 MouthOpen=0.5／MouthForm=1 的中间预览，不是闭态。嘴形中点、笑眼及组合形变仍需精修，不算三组样机验收通过。27 个默认普通参数或相同的空关键形均不计有效参数。
- 当日原生保存检查点为 `xiaoya-native-eighteen-left-smile-checkpoint-20261009.cmo3`，官方保存于 **23:16:27**，**1,369,215 字节**，SHA-256 为 `3bd8c20b33318a4e96aa727764ddf53164a18a462aa1ea49374dbc9295db665a`；另保留 6、7、10、14、17 项绑定阶段检查点。兼容导出和工程往返必须对最终候选重新执行，不能沿用历史路线证据。
- 官方手动网格编辑“4 等份”已将左眼白从 4 顶点加密为 9 顶点，现场确认原睁眼轮廓与 EyeOpen=0 的薄闭眼键形保留，随后给其新父曲面 Warp6 绑定 `ParamEyeSmile=0／1` 并提升三个中间控制点产生曲率。这些修改已一同官方保存。闭眼弧线仍夹杂暗色虹膜片段，须补瞳孔闭态透明度和右眼笑眼，不能以已产生形变代替样机通过。`ParamBodyAngleX` 仍无有效绑定。
- Editor 5.3 的“建模→参数→重置为默认值”菜单已实际成功调用。其默认 Ctrl+1 与本机 PixPin 取色快捷键冲突，曾产生全屏浮层遮挡输入；官方 `sky` 对 PixPin 的临时取消入口返回 `Computer Use was not approved to use PixPin`，未访问或重开该应用。**22:32 最新 Editor 只读截图已经确认浮层消失，原应用许可保持不变，继续制作无需新增 PixPin 授权**；后续只调用 Editor 菜单，避开 Ctrl+1。之后真实用户输入曾两次触发保护，均刷新且不按失效截图输入；用户明确答复“现在可以继续制作模型”后，最新无遮挡截图确认恢复，并实际完成上述左眼曲率。
- 本轮后续再次遇到新的截图故障：`encode latest capture frame failed: window crop is outside captured monitor`。刷新真实窗口选择并按官方恢复规则重试后仍失败；官方文字状态可确认当前同源工程与焦点，因此通过 `sky.press_key` 的 Ctrl+S 保全了这次原生形变。文字树的最大化按钮没有可执行 secondary action，实际点击返回 `element 5 has no cached bounds`；Alt+Space 未在文字树中呈现系统菜单，未猜测菜单键。当前已停止几何输入，待 Editor 恢复可截图位置；未操作 Ja、其他应用或显示器配置，也未关闭 MCP 会话。
- 已保留 `xiaoya-native-27-parameters-checkpoint-20261009.cmo3` 及官方导出的 `official-parameters-before.csv` 原始 GUID；`official-parameters-aligned.csv` 已经官方非删除导入，仅对齐本任务目标 ID／名称／范围，不代表新增有效绑定。`official-parameters-final-target.csv` 保留原始 GUID、ID 与范围，仅准备目标 19 行，尚未导入，不能作为当前工程已减为 19 参数的证明。8 个无绑定默认参数将在新候选内清理，历史源工程、原 PSD、PNG 及旧运行包全部保留。
- 闭眼笑眼视线、闭嘴嘴形、挥手头身三组完整样机、19 有效普通参数／0 BlendShape、35 组合、该新候选往返及兼容导出、浏览器和正式晋升均尚未通过。旧运行包保持不变。

当日许可标题明确显示既有试用剩余 **40 天**，实际 Help 菜单的“注销许可证”不可用。[官方 FAQ](https://help.live2d.com/other/other_12/)说明试用无法在期限届满前提前停用。未购买、未启用新试用、未改许可证或机器记录；可继续官方制作，但 **FREE 模式实际保存尚未验证**，试用保存不会作为该项证明。

无损容器准备工具为 `../tooling/prepare_editor_psd.py`，参数 `--reverse-layers` 仅用于本次源素材的绘制顺序整理。输出包含每层原通道 SHA-256，准备报告本身不代表官方导入或模型验收通过。格式依据为 [Adobe PSD 规范](https://www.adobe.com/devnet-apps/photoshop/fileformatashtml/)。

## 历史记录：本次升级早期入口检查（已恢复）

已读取已安装的 `computer-use` 26.1002.52244 技能及其 `guidance.md`、`confirmations.md`。技能指定 Windows 应用只能通过 `node_repl` 中的 `@oai/sky` 操作。本次会话实际工具清单没有 `node_repl`、`sky`、Computer Use 或工具搜索入口，依赖定位工具返回的普通 Node.js 运行时也不能代替指定的应用操作入口。当前进程清单未发现可观察的 Cubism／Live2D Editor 窗口进程。

这是恢复前的检查记录。当时未向 Editor 输入，阻塞发生在可调用的操作通道缺失。之后已重新列举真实窗口并恢复观察和制作，当前状态以上方本次进展为准；未执行正式晋升，历史坐标不复用。

## 后续验收

本机保留 `native-progress-20261009.json` 与 `native-acceptance-checklist.md` 的历史制作及检查顺序，当前通过项和剩余门限以上方 2026-10-11 状态为准。详细回执和原始证据不随公开源码提交。

局部网格、父子绑定、开闭态与中间插值、三组组合样机及动态表现已通过本次技术验收；下一门为真实 FREE 保存。不能用试用保存替代，也不缩减动作或放宽标准。

新工程、MOC／纹理／model3、海报、表现配置和参数绑定清单作为一个资产包整体验收。必须实际打开、保存、关闭、重开、兼容导出并在浏览器验证后才晋升；通过前不混用新旧资源，不删除旧运行包或历史制作路线。

数量与制作约束见 [官方 FREE 限制](https://www.live2d.com/en/cubism/comparison/)，网格操作见 [官方手动编辑说明](https://docs.live2d.com/zh-CHS/cubism-editor-manual/mesh-edit-manual/)。本轮代码与设备验收分别见 [分层验收记录](../../../../docs/delivery-acceptance.md)。
