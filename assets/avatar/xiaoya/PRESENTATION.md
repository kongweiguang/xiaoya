# 内容与语气联动：模型候选和官方 Editor 接力

## 本轮状态（2026-10-07）

表现控制器、六份运行表情资源和定向测试已完成；新增真实笑眼、眉角和明显手臂动作的模型仍是独立候选。**本轮官方 Editor 打开、另存、关闭重开、SDK 5.0 导出尚未完成，不能称为正式模型升级或生产级完成。** 当前会话的原生桌面自动化不可用，不能沿用历史会话的授权桥接脚本。

正式 MOC 仍为 `e6684a201f6b5038779b498680fdae08038430f21f0235628cf9651142096cda`，正式 CMO/PSD 和运行二进制没有被候选替换。正式 16 参数包的表情配置现有六份；`gentle/curious` 已能通过现有眼睛开度、抬眉和视线表现，真实笑眼、眉角和 30° 手臂幅度必须等本候选通过官方门后生效。

| 候选检查 | 本轮结果 |
| --- | --- |
| 源资产 | 16 图层、19 参数、6 旋转变形器、1113 几何关键形态、17 内嵌图片 |
| 真实形变 | 19 参数均改变官方 Core 几何；新增 `ParamEyeSmile`、`ParamBrowLAngle`、`ParamBrowRAngle` |
| 面部连续性 | 36 组笑眼／开合／眉角组合无非有限值或翻面；闭眼、满笑瞳孔允许退化为线 |
| 可见幅度 | 满笑眼弧曲率约 26 源像素；手臂最大位移约 121.35 源像素 |
| 接合旧门 | 132 姿态全部通过；最小颈／左肩／右肩重叠 2115／4257／3609 源像素²；耳座不移动且在头壳后绘制 |
| 资源 | 15 引用、6 表情、2 历史 motion；嘴部仍由音频独占 |
| 浏览器 | 实际 Edge、Runtime、Core 和 WebGL；五表情、挥手、停止归零、小屏无溢出、无页面或控制台错误 |
| 未验证 | 本轮官方 Editor；候选与官方导出几何一致性；正式包晋级；真实房间与真人设备 |

浏览器用本地合成口型数值、拦截映射本地资源，不启动新服务、不连接模型供应商，不能代替真实 TTS/LiveKit 房间验收。运动使用唯一合成器和短固定曲线，不启动第二个 motion 引擎：基线 → 资源表情／一次性动作 → 实际口型包络轻摆／叶片物理 → 音频嘴部最后写入。资源 `Add` 永远相对当帧基线，不累计上一帧；打断清动作，同一 delivery ID 不重播手势。

## 已冻结的输入

请使用下面这份文件，不要打开旧正式 CMO，也不要重跑导出器覆盖冻结候选。

```text
C:\dev\rust\xiaoya\.tools\model-export\presentation-2026-10-07\candidate\xiaoya.cmo3
```

- CMO SHA256：`6d11468e1e349ee802b7bdd135d832c382e885b4041f38346a7f90bb74509b98`
- 独立候选 MOC SHA256：`bae6525ae71976e6134e315738b47610caaf11b86d1b35ff5614a671d8d743a1`
- 候选 PSD SHA256：`d944d73590a4406c6bb4d30166cf2985e3e7a4fd81973de3e2bcb252191f872b`

独立候选 MOC 为 Cubism 3.0 格式，仅作几何与浏览器预览，**禁止复制到正式运行目录**。

## 人工官方 Editor 步骤

本机已安装 Cubism Editor 5.3.04。无须下载工具、变更服务或购买许可；如果当前许可不允许保存/导出，请停在该处并反馈原始提示。

1. 在官方 Editor 中打开上面的 `candidate\xiaoya.cmo3`。若出现版本／参数分组修复提示，记下提示后按正常官方流程处理；若提示缺失贴图、无法读工程或模型损坏，停止，不要自行删除零件或重新自动排图集。
2. 核对完整人物、16 个 ArtMesh、19 个参数和 6 个旋转变形器。新增三参数必须可见；可以短暂预览笑眼、眉角和左右手。不要编辑网格或重新计算变形器；如果参数预览引起未保存标记，丢弃预览更改，再从冻结候选重新打开。
3. 用 **File → Save As／文件 → 另存为** 保存到下面的隔离工作工程，绝不能覆盖 `assets\avatar\xiaoya\xiaoya.cmo3` 或冻结候选：

   ```text
   C:\dev\rust\xiaoya\.tools\model-export\presentation-2026-10-07\editor-final\source\xiaoya.cmo3
   ```

4. 关闭该模型标签，再打开刚保存的 `editor-final\source\xiaoya.cmo3`。确认不再出现首次版本／分组修复提示，人物和参数都正常。保存与重开须真实执行，不能只观察文件存在。保留这一屏和任何提示的截图；如果继续预览参数，导出前丢弃未保存预览并再次重开这份已保存工程。
5. 打开纹理图集确认只有一张 **2048 × 2048** 的纹理。候选已排好图集，不要自动重排、重新缩放零件或替换纹理。若尺寸不是 2048，停止反馈，避免把错误图集带入导出。[官方图集说明](https://docs.live2d.com/en/cubism-editor-manual/texture-atlas-edit/)
6. 在干净重开的工程中，执行 **File → Export Embedded File → Export as MOC3 file**（文件 → 导出嵌入用文件 → 导出 MOC3；默认快捷键 `Ctrl+Alt+S`）。[官方菜单说明](https://docs.live2d.com/en/cubism-editor-manual/file-menu/)
7. 导出设置逐项核对：版本选 **SDK 5.0 / Cubism 5.0**，类型选 **SDK**；目标选 **1/1（2048 px）**。模型中心 X/Y 保持 **0.50 / 0.50**；1280 × 1280 画布对应单位宽／高 **1.00 / 1.00**，确认自动计算的 **pixelsPerUnit = 1280**。如果该字段可编辑，也应复核单位宽高没有变化。勾选 **Defringe／防溢色**，即 `Apply color leakage prevention processing to the texture`。MOC、model3 和纹理默认导出；可勾选 CDI 显示信息。候选的 physics/MotionSync 由项目资源保留，不能用空设置覆盖它们。保存这张设置截图。[官方导出设置说明](https://docs.live2d.com/en/cubism-editor-manual/export-moc3-motion3-files/)
8. 输出文件名使用 `xiaoya`，导出位置只选下面隔离目录：

   ```text
   C:\dev\rust\xiaoya\.tools\model-export\presentation-2026-10-07\editor-final\runtime\xiaoya.moc3
   ```

   Editor 可能把纹理放在 `runtime\xiaoya.2048\texture_00.png`，这是正常的；保留 Editor 生成的全部文件，不手改 JSON、不重命名或重存 PNG。防溢色依赖透明像素里的颜色，图片软件重新保存可能破坏它。

9. 等待导出成功。如果 Editor 为导出配置产生未保存标记，正常保存隔离的 `editor-final\source\xiaoya.cmo3`，再关闭重开核对一次，不覆盖其他路径。告知主任务“官方导出完成”，附上保存／重开／导出设置截图或提示。**到这里停止，不自行替换正式包。**

目标兼容版本区别于 Editor 自身版本；不要选最新版 SDK 5.3、SDK 3.0 或仅视频目标。本项目运行 Core 仍是 5.0。[官方目标版本说明](https://docs.live2d.com/en/cubism-editor-manual/target-version-selection/)

## 主任务接手后的门槛

1. 读取本轮 `editor-final/source` 与 `editor-final/runtime`，核对新哈希、官方 MOC 版本、2048 PNG 和 1280 pixelsPerUnit；历史 `evidence/editor-verification.json` 不能作为本轮完成证据。
2. 使用 `ValidateSource` 读回官方保存 CMO 与同目录 PSD/图层，确认 19 参数、16 ArtMesh、6 变形器、1113 关键形态和内嵌图片。官方保存可能规范化工程，因此 CMO 字节哈希不要求等于候选。
3. `validate-core.cjs` 检查官方 MOC 全参数、24 口型组合和 36 面部组合。`validate-equivalence.cjs` 对照冻结候选与官方 MOC，现为 **117 姿态**，含新增表情组合，最大源像素差不得超过 0.01。
4. 从候选复制配置、表情、物理、MotionSync、静态图到**独立 staging**，只替换官方导出的 MOC、纹理、CMO；处理 Editor 的纹理路径，但保留中文 CDI 和六个表情引用。不能把 Editor 新建的精简 `model3.json` 直接覆盖正式配置。
5. 对 staging 运行 `validate-package.cjs`、原有 132 姿态 `validate-connections.cjs` 和 `verify-presentation.cjs`。继续检查源 PSD/CMO、MOC、纹理、后备图及配置的整体对应，保留旧正式包可恢复副本后再统一晋级；不得把候选/官方两代文件混着发布。
6. 晋级后重新验收实际应用及私有房间音频、语义 cue、打断、静音、重连和静态后备，再更新正式 README／哈希／本轮证据。没有真人设备验证时仍须明确注明。

下面两个检查可先对官方原始导出执行，不修改正式资源：

```powershell
node assets/avatar/xiaoya/tooling/validate-core.cjs .tools/model-export/presentation-2026-10-07/editor-final/runtime/xiaoya.moc3 web/public/avatar/vendor/live2dcubismcore.min.js .tools/model-export/presentation-2026-10-07/editor-final/evidence/core-validation.json
node assets/avatar/xiaoya/tooling/validate-equivalence.cjs .tools/model-export/presentation-2026-10-07/candidate/xiaoya.moc3 .tools/model-export/presentation-2026-10-07/editor-final/runtime/xiaoya.moc3 .tools/model-export/presentation-2026-10-07/editor-final/evidence/geometry-equivalence.json
```

本轮候选全部证据在 `C:\dev\rust\xiaoya\.tools\model-export\presentation-2026-10-07\evidence`；它们带有未含官方 Editor／未含私有房间的边界，不能改名冒充正式验收。
