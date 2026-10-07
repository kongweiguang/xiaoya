# 小芽角色素材

使用内置 imagegen 工具于 2026-10-05 生成。正式资源为 8 张 1024×1024 RGBA PNG，具有真实透明背景。母版和各表情的相机、比例、位置、身体及配色保持一致。原始工具画布仅作等比尺寸规范，不重新绘制角色。

## 母版提示词

Production animation character master PNG for a Chinese voice assistant called 小芽 (Xiaoya). ONE adorable original small toy robot, front-facing, centered, full body, orthographic camera, square canvas with actual transparent background. Rounded creamy ivory body and head, two small mint-green ear pods shaped like sprouts, dark charcoal face display, two friendly luminous cream eyes, tiny CLOSED curved smile, delicate peach blush, short rounded arms hanging down and tiny feet. Soft 2.5D toy illustration, satin ceramic/plastic finish, subtle soft shading, minimal elegant detail. Warm, welcoming, cute without being childish. About 70% canvas height and 58% width, generous transparent margin, level foot baseline. No floor, ground shadow, props, text, logos or watermark.

## 派生表情提示词

每张均引用 `idle.png` 的原始生成母版，以 precise-object-edit 生成。共用约束：Only modify eyes or mouth inside the existing dark face display. Preserve robot identity, body, ears, head shell, blush, lighting, material, scale, silhouette, position, camera and framing. Actual transparent alpha background. No new text, logos, props or graphics.

| 文件 | 表情变化 |
| --- | --- |
| idle.png | 母版：睁眼、闭嘴微笑 |
| blink.png | Gentle closed curved eyelids; closed smile unchanged |
| greeting.png | Happy crescent eyes and warmer closed smile; arms remain down |
| listening.png | Slightly wider attentive eyes; mouth closed |
| thinking.png | Eyes looking slightly upward with thoughtful eyelids; closed neutral mouth |
| speaking-small.png | Small open rounded mouth; master eyes unchanged |
| speaking-open.png | Larger open oval mouth; master eyes unchanged; no teeth or tongue |
| confused.png | One eye slightly narrowed; tiny asymmetric closed smile; friendly puzzled expression |

运行时由 Pillow 预合成 512×512 奶油色舞台，每个表情仅有一个固定位置的画面，视频为 15 fps。所有画面以 `idle.png` 为不变底图，仅在有界的五官区域融合派生素材；说话只更新嘴部，眼睛、身体、轮廓、腮红和背景逐像素保持一致。派生生成图仍存在细微的明暗及边缘差异，不能直接整图轮播，否则会闪烁。局部遮罩边缘柔化以避免拼接线，加载时在线程中完成，运行时不再进行图像处理。

角色不做上下浮动，只切换眨眼、嘴型和表情。嘴型是粗动画，并非音素口型。网页 `public/avatar/idle.png` 与正式母版字节相同；更新后运行 `uv run python deployment/prepare-avatar-assets.py` 同步。
