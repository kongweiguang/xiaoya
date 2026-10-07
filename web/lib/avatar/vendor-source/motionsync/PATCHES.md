# MotionSync 官方来源与本地补丁

来源：官方 `CubismSdkMotionSyncPluginForWeb-5-r.2/Framework/src`，固定 5-r.2。
版权、许可及官方接口保持原始声明。生成的 JavaScript 与声明文件通过
`pnpm exec tsc -p tsconfig.motionsync.json` 输出至 `vendor/motionsync`。

本地修复仅针对真实运行所需的内存边界和释放路径：

- 分析结果结构固定使用三个 Int32 字段，不把模型参数数量当作结构长度。
- CRI 分析配置按字节分配；临时配置每次分析结束释放。
- 处理器关闭释放持有的分析结构。
- 映射销毁释放组合缓冲及嵌套的字符串、数值映射。
- 创建上下文后释放临时上下文配置，销毁引擎时释放最后一批输入。

业务层使用公开数据解析、引擎、处理器与结果接口，按采样消费数量保留音频时间戳。
未修改 Core 分发文件，也不访问 LiveKit 的内部音频方法。
