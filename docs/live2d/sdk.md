# Live2D SDK 固定版本、来源与授权

本项目使用官方稳定发行包的 Cubism Web SDK `5-r.3` 与 MotionSync Web Plugin `5-r.2`，同源托管 Core，按固定源码构建 Framework。小芽原画、绑定和模型另见 [制作说明](../../assets/avatar/xiaoya/README.md)，没有在正式页面发布官方样例人物。

## 固定发行包

| 内容 | 官方发行包 | 实际运行版本 | 固定下载地址 |
| --- | --- | --- | --- |
| Cubism Web | `CubismSdkForWeb-5-r.3.zip`，2025-02-18 | Cubism Core `5.0.0` | [官方 ZIP](https://cubism.live2d.com/sdk-web/bin/CubismSdkForWeb-5-r.3.zip) |
| MotionSync Web | `CubismSdkMotionSyncPluginForWeb-5-r.2.zip`，2025-03-27 | `Live2DCubismMotionSyncEngine_CRI` `5.0.4` | [官方 ZIP](https://cubism.live2d.com/motion-sync-plugin/bin/CubismSdkMotionSyncPluginForWeb-5-r.2.zip) |

下载入口为 [官方 Cubism Web 下载页](https://www.live2d.com/en/sdk/download/web/) 与 [官方 MotionSync 下载页](https://www.live2d.com/en/sdk/download/motionsync/)。固定 URL 已核对可用；下列 SHA256 是本次实际下载包的指纹，用于后续同步相同文件，不表示第三方签名验证。

```text
CubismSdkForWeb-5-r.3.zip
c70cc086950c7a318515e8ee606d8458a6dd2b96773fc2514ff67bf8a2d9ead7

CubismSdkMotionSyncPluginForWeb-5-r.2.zip
74dc9236e28263f9cba7962704ce61ad06677e79ca6e56657f216a098015452f
```

官方包内 `cubism-info.yml` 记录 Cubism Core commit `1ca7600dd547d156253674aa51b75ae76fea014b`、Framework commit `c30da6695f9c44c3fa8805bd34f6318423be4e3c`；`cubism-motionSync-info.yml` 记录 MotionSync Core commit `d9847bdf117e1a643c3d81bb5a92a48cc32599d7`、Components commit `4e7daecd27712379aa522536bf041e1202d9ef1c`。以发行包为依赖来源，不使用官方仓库未发布的 main 功能。

## 项目内文件与校验

以下文件的 SHA256 与对应官方 ZIP 解压文件逐一相同，Core 未修改。公共版本 API 实测 Cubism 为 `5.0.0`，CRI 为 `5.0.4`（原始版本号 `83886084`）。

| 项目内文件 | SHA256 |
| --- | --- |
| `web/public/avatar/vendor/live2dcubismcore.min.js` | `944988f1523c9f888afe3611b630121ff503ea6246971d2eeee76f45cbfb8004` |
| `web/public/avatar/vendor/live2dcubismmotionsynccore.min.js` | `60e2a8ba9b422a0f8a3d7e066739352e9b903cc1011339984ad922e80a3cd19a` |
| `web/types/live2dcubismcore.d.ts` | `fa914858b76cf0589be2a120dfbd6733951f725f79139342d4908af05c3ed2dd` |
| `web/types/live2dcubismmotionsynccore.d.ts` | `a4aedace54df065bcd2d93115021b3fdd580c36cf76e3724b02ef084ffccca5e` |

Cubism Framework 源码位于 `web/lib/avatar/vendor-source/cubism/`，MotionSync Components 源码位于 `web/lib/avatar/vendor-source/motionsync/`；`pnpm sdk:build` 编译至 `web/lib/avatar/vendor/`。官方源码的版权和许可证声明保留。MotionSync 的本地内存边界和释放修复见 [PATCHES.md](../../web/lib/avatar/vendor-source/motionsync/PATCHES.md)，该补丁记录继续保持有效，Core 分发脚本没有修改。

## 许可证及再分发记录

Cubism Framework 与 MotionSync Components 使用 [Live2D Open Software 使用授权协议](https://www.live2d.com/eula/live2d-open-software-license-agreement_cn.html)。Cubism Core 与 MotionSync CRI Core 使用 [Live2D Proprietary Software 使用授权协议](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_cn.html)。MotionSync 的官方说明包含：Powered by "CRIWARE". CRIWARE is a trademark of CRI Middleware Co., Ltd.

许可文本、链接和再分发清单已随交付保留在同源静态目录 `web/public/avatar/vendor/`：

| 文件 | 官方包内来源 |
| --- | --- |
| `CUBISM-SDK-LICENSE.md` | Cubism Web `/LICENSE.md` |
| `CUBISM-CORE-LICENSE.md` | Cubism Web `/Core/LICENSE.md` |
| `CUBISM-CORE-REDISTRIBUTABLE.txt` | Cubism Web `/Core/RedistributableFiles.txt` |
| `CUBISM-FRAMEWORK-LICENSE.md` | Cubism Web `/Framework/LICENSE.md` |
| `MOTIONSYNC-SDK-LICENSE.md` | MotionSync Web `/LICENSE.md` |
| `MOTIONSYNC-COMPONENTS-LICENSE.md` | MotionSync Web `/Core/LICENSE.md` |
| `MOTIONSYNC-CORE-LICENSE.md` | MotionSync Web `/Core/CRI/LICENSE.md` |
| `MOTIONSYNC-CORE-REDISTRIBUTABLE.txt` | MotionSync Web `/Core/CRI/RedistributableFiles.txt` |

原有 `web/public/avatar/vendor/LICENSE.md` 同样是官方 MotionSync CRI Core 许可，予以保留。两个 Framework 源码目录各自保留 `LICENSE.md`。复制文本保留官方内容，统一换行不改变条款。

官方 Core 再分发清单允许 Cubism 的 `.d.ts`、`.js`、`.min.js`，以及 MotionSync 的 `.d.ts`、`.js`、`.js.map`、`.min.js`；本项目选用其中 `.min.js` 与 `.d.ts`，不为运行时发布整套 SDK 或样例资源。

2026-10-06 对当前文件重新校验，4 个 Core/声明文件与官方 ZIP 解压文件逐字节一致，8 个许可与再分发清单在统一换行和文件末尾空行后内容一致，两个固定 ZIP 指纹与上表相同。当前证据见 [sdk-source-validation.json](../../assets/avatar/xiaoya/evidence/sdk-source-validation.json)。

发行包许可要求符合其业务规模条件的企业取得 [Cubism SDK 发行许可证](https://www.live2d.com/zh-CHS/download/cubism-sdk/release-license/)。这份记录说明技术来源和随附许可，不代表已取得企业发行合同；本次范围为本地应用与私有房间验收，公网发布不在交付范围。

## 模型制作验收与 SDK 验证边界

2026-10-07 的正式模型已经在官方 Cubism Editor 5.3.04 中打开、另存、关闭重开并导出。发布格式为 SDK 5.0 / Cubism 5.0，纹理 2048，pixelsPerUnit 1280，官方 Core 一致性通过；制作候选与官方运行导出的 93 种姿态几何差异为零。工程保留 16 个网格、16 个参数、6 个变形器和 645 个关键形态。头颈、肩部与头饰连接及原画边缘精修已同步至 PSD、CMO、MOC、纹理和后备图。模型完整证据见 [制作说明](../../assets/avatar/xiaoya/README.md) 与 [editor-verification.json](../../assets/avatar/xiaoya/evidence/editor-verification.json)，应用验收单独见 [验收记录](acceptance.md)。

本机 Editor 位于 `C:\Users\24052\AppData\Local\Programs\Live2D-Cubism-5.3`，2026-10-07 耳座修复时为剩余 42 天的试用版，未购买长期 Editor 许可。Editor 制作许可和 SDK 发行许可分别适用；完成本地技术验收不代表已经取得企业 SDK 发行合同。

## 更新约束

更新 SDK 时重新核对官方稳定发行包、ZIP 与 Core 指纹、声明文件、兼容性、再分发清单及许可证，保留 MotionSync 补丁所对应的验证证据，再运行 `pnpm sdk:build` 和项目规定的前端检查。模型资产当前指纹记录在 [asset-sha256.json](../../assets/avatar/xiaoya/evidence/asset-sha256.json)，SDK 文件不使用 pnpm CDN 动态加载。
