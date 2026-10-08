# 参数与表现约束

外部参数为 19 个，普通参数范围与当前运行契约一致：

- 头身：ParamAngleX、ParamAngleY、ParamAngleZ、ParamBodyAngleX。
- 眼与视线：ParamEyeLOpen、ParamEyeROpen、ParamEyeBallX、ParamEyeBallY、ParamEyeSmile。
- 眉：ParamBrowLY、ParamBrowRY、ParamBrowLAngle、ParamBrowRAngle。
- 嘴：ParamMouthOpenY、ParamMouthForm。
- 附肢与呼吸：ParamLeafSwing、ParamArmL、ParamArmR、ParamBreath。

风格为 neutral、happy、gentle、concerned、curious、shy、surprised。手势为 nod、tilt、wave、shy、shake，none 不触发动作。35 种风格／手势组合都必须实际检查，不以单项参数存在代替绑定有效。

实际音频口型独占嘴参数，表情和手势不能写入嘴；停止播放／取消后口型归零。眼、眉、视线、叶片、手臂与头身可组合，采用父子变形器隔离职责，避免普通参数组合冲突。

参考原画张嘴微笑；静音默认必须闭嘴。两者分别核对形象，不要求静音姿态与原画逐像素一致。图集可重新排布，验收关注外观、有效绑定和浏览器运行结果。
