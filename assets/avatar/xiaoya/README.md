# 小芽模型资产

`concept.png` 是保留的原画。2026-10-15 当前网页运行包为 `web/public/avatar/xiaoya/native-v1`：同形象的官方 5.3.04 分层导出，19 普通参数、0 BlendShape、18 个独立部件。旧单张整体网格只能产生很小的肢体挪动，保留作回退，不再作为动作已可见的证明。

运行 MOC 为 SHA-256 `601c6d70eab756fd2265973533a3315b0e7fc5440770a0effd2495c300bae0dc`；来源为已验收保护工程 `stable-5.3/xiaoya-native-final19-checkpoint-20261011.cmo3`（`003f552f4cc9ba1409fa8e95a86acdd7a345315f79935876325da913dc55b9e7`）。同名工作工程后来发生变化，不借其当前身份证明旧导出。完整资源包含官方 MOC／纹理／显示参数、项目表情／MotionSync／物理／微动配置，以及当前渲染器直接导出的中性透明海报。此次晋升复用官方技术验收；历史 FREE 实际保存仍待验，未改变许可或文件头。

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

当前包固定检查浏览器实际使用的 `native-v1/poster.png`，报告包含该路径及实时 SHA；未使用的根 `poster.png` 不能替代它。候选包用 `--poster <包内相对路径>` 明确声明实际后备图，不根据 MOC 位置猜测、不搜索或回退其他图片。正式资产包晋升时，浏览器引用与检查入口的当前海报约定一起更新。

`--stable` 必须 19 普通参数、0 BlendShape，当前包严格执行该门。35 组组合使用有效 v3 指令并检查开始／完成和相对同风格无动作对照的实际顶点位移；独立手臂还须超过可见幅度门。前端物理检查遍历全部部件三角与不透明纹理面，拒绝旧整体网格。外观、真实对话及官方工程往返分别留证，参数或回执单独通过不等于用户看到了动作。
