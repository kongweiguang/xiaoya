# 小芽模型资产

`concept.png` 是保留的原画。当前网页运行模型仍保留在 `web/public/avatar/xiaoya`，现有工程和历史证据不能充当本轮稳定版重建通过的证明。

## 稳定版重建目标

只在已安装的官方 Cubism Editor 5.3.04 中从原画新建。19 个普通参数、0 BlendShape；外部 ID／范围与 `web/lib/avatar/presentation.ts` 一致。采用局部网格与父子变形器，不重画角色，不锁定旧单网格、顶点数量或纹理字节布局。必须符合 FREE 限制，不购买或启用试用。

先完成三个组合样机：闭眼＋笑眼＋视线、闭嘴＋嘴形变化、挥手＋头身运动。样机失败不晋升正式包，不删除旧包，不缩减动作。原画参考姿态与静音闭嘴姿态是不同基准。

正式包必须包含源工程、兼容导出的 MOC／纹理／model3、海报、表现配置及参数／绑定清单。官方 Editor 实际打开、保存、关闭、重开、兼容导出通过后，再检查 Core／浏览器，包含单项与组合极值、35 种风格／手势组合、口型优先级和停止恢复。

不能修改版本头伪造 Editor 兼容，也不能用 Umamo／Java 序列化生成器代替官方工程往返。旧工具与资产待新包通过后才清理；当前保留仅为可恢复历史，不是新交付路径。制作遵循 [官方 FREE 限制](https://www.live2d.com/en/cubism/comparison/) 与 [兼容导出说明](https://docs.live2d.com/en/cubism-editor-manual/export-moc3-motion3-files/)。

本轮状态见 [分层验收记录](../../../docs/delivery-acceptance.md)，表现规则见 [参数与表现](PRESENTATION.md)。

当前包统一静态／Core／35种表现组合检查入口：

```powershell
node assets/avatar/xiaoya/tooling/validate-model.mjs --output .tools/logs/refactor-model.json
node assets/avatar/xiaoya/tooling/validate-model.mjs --model-dir <candidate-package> --poster poster.png --stable
```

当前包固定检查浏览器实际使用的 `concept-v1/poster.png`，报告包含该路径及实时 SHA；未使用的根 `poster.png` 不能替代它。候选包用 `--poster <包内相对路径>` 明确声明实际后备图，不根据 MOC 位置猜测、不搜索或回退其他图片。正式资产包晋升时，浏览器引用与检查入口的当前海报约定必须一起更新。

`--stable` 必须 19 普通参数、0 BlendShape；当前旧运行包不满足这个门，不能据此宣称稳定版通过。该入口不约束单网格、顶点数量或纹理字节，只检查真实参数、网格绕序、资产引用和嘴参数独占；外观与官方工程往返仍需单独留证。
