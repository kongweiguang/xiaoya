# 网页角色

网页只维护 Live2D 角色运行链路，不再发布旧 PNG 视频流。保留小芽原画、现有配色及桌面双栏／手机上下布局。

角色 GPU／模型归独立渲染器所有，换房间只重绑音频分析；`RoomAudioRenderer` 是唯一可听出口。角色懒加载，减少动态效果、静态后备和口型降级继续有效。

官方 SDK 源码与授权见 [SDK 记录](sdk.md)，模型制作与稳定版验收见 [模型资产说明](../../assets/avatar/xiaoya/README.md)。当前唯一验收入口为 [分层验收记录](../delivery-acceptance.md)，旧报告在本机归档，不随公开源码提交，也不作为本轮通过依据。

```powershell
pnpm --dir web install --frozen-lockfile
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
```

需要 SDK 的命令先生成产物，不提交 generated JS／类型声明；Core、源代码、必要补丁和许可证保留。私有运行使用 [部署说明](../../deployment/README.md)，不使用旧同步或前台启动路线。
