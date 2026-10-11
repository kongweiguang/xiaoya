# 人物表现验收工具

`evaluate_jev.py` 复用正式后端的 HTTP 适配器、判断原语解析及领域仲裁。138 条固定中文样例包含 30 条明确正向动作、20 条否定、15 条引用、15 条假设、20 条目录外动作、20 条情绪、12 条倾听及 6 条思考。每种注册手势都有 6 条正向样例。

## 离线检查

```powershell
uv run python scripts/evaluate_jev.py
```

默认只检查样例类别、唯一 ID、阶段及期望枚举，不读取 Key、不联网；输出不会声称中文效果已经通过。

## 离线 Mock 契约验收

```powershell
uv run python scripts/evaluate_jev.py --mock --output .tools/jev/mock-contract-report.json
```

此模式对 138 条人工样例构造标准 `Choice`／`Score`／`Noul` 响应，经过正式解析器与统一表现仲裁。它验证接口契约、阶段约束和验收统计，不模拟真实模型的语义理解或网络延迟；报告明确标记 `mock-contract`，线上效果保持未执行。

## 显式线上验收

先在被忽略的 `.env.local` 填写 `VOICE_AGENT_JEV_API_KEY`，然后执行：

```powershell
uv run python scripts/evaluate_jev.py --live --output .tools/jev/chinese-report.json
```

只有 `--live` 会把固定测试样例发送给官方 `https://api.typesafe.ai/v1/systemone`，使用 `jev-1.13.0` 并消耗接口额度。不会发送用户音频、原会话或其他配置。可用 `--env-file <path>` 指定配置；已有进程环境变量优先于文件。

每条样例输入不预置动作，防止预设手势冒充 Jev 判断。倾听采用 700ms 总预算，其他阶段采用 900ms，表情和动作总是经过正式阶段约束仲裁；视觉表现不被旧语音风格族限制。

报告包含 ID、分类、判断标签与分数，不记录测试原文、Key 或第三方响应正文。统计内容：

- 已采纳判断正确率与整体采纳覆盖率，另列风格和手势精度。
- 正向动作命中覆盖率，漏采纳在此计分，不伪装成已采纳动作的精度错误。
- 否定、引用、假设和目录外动作的额外手势误触发数量。
- 降级数量以及成功网络请求的 p50／p95；被熔断跳过的请求不算网络耗时样本。

通过要求：全部 138 条完成、没有降级、已采纳判断正确率至少 95%、正向动作覆盖率至少 80%、关键误触发为零。无采纳样本显示 `null` 并失败，不以零除处理制造满分。

退出码：`0` 为离线校验、Mock 契约或线上门通过；`1` 为 Mock 或线上门未过；`2` 为配置或样例文件错误。此工具不证明完整语音首声速度、物理设备回声或 Live2D 工程质量，这些必须分别验收。

接口与模型约定来源：[TypeSafe API](https://docs.typesafe.ai/api)、[模型与语言说明](https://docs.typesafe.ai/models)。

## Jev 并行首帧验收

```powershell
# 完全离线：正式合成节点 + Mock TTS + 500ms Mock Jev
uv run python scripts/benchmark_presentation.py
# 显式访问已配置私有 TTS；Jev 仍为 Mock，不调用 DeepSeek
uv run python scripts/benchmark_presentation.py --private-tts
```

默认每种模式各 5 次暖机、30 组交替配对。完整消费实际 SDK AudioFrame，分别计算无 Jev、Mock Jev 的 p50/p95 和每对增量，并核对流、客户端与表现任务回收。验收比较 **两模式 p95 的差**是否不超过 100ms；每对差值的 p95 另列波动诊断，两者不能互换。报告在被忽略的 `.tools/presentation-benchmark/`；`--output` 可指定该目录下的位置。

测量起点为正式 `_synthesize` 被消费，终点为首个 AudioFrame；它是服务端合成首帧，不能代替用户说完到浏览器实际出声。有效迟到决策与超时探针另行离线执行。真实 Jev 网络分位数只来自上面的 `--live` 中文验收，不由固定 500ms Mock 生成。

## 中文轮次六组合诊断

```powershell
# 默认只检查素材、覆盖与预算；不加载模型、不读取私有识别配置
uv run python scripts/benchmark_turns.py
# 真实 CPU Silero / 本地 v1-mini；识别文本与固定回复采用离线重放
uv run python scripts/benchmark_turns.py --offline-replay --repeats 1 --warmups 1
# 正式 Paraformer WebSocket 适配器；仍使用固定回复，不调用 DeepSeek、Jev 或 TTS
uv run python scripts/benchmark_turns.py --private-stt --repeats 1 --warmups 1
```

矩阵固定最小轮次等待 `0.60／0.75／0.90s`、VAD 打断时长 `0.35／0.50s`，最大等待 `1.10s`。34 个合成场景覆盖短音与连贯中文、完整句间停顿、20／10dB 噪声、纯背景噪声、早期声学重叠以及三条问题的 `0.40／0.60／0.80s` 句内停顿。每重复共 204 个正式场景，另有每组合暖机。默认三重复；一重复是筛查，不能据此宣布生产参数最优。

三条不完整问题的前后半句由 `prepare_turn_materials.py --private-tts` 显式调用配置的私有 TTS 生成；完整消费六条合成流再关闭客户端，仅裁剪 WAV 音频，保留源文件、原文、裁剪位置与 SHA256。该工具不会自动运行，素材清单明确 `human_listened: false`，没有把机械声学边界检查称作人工试听。素材缺失或摘要不符会阻止完整覆盖通过。

实际运行使用公开 `AgentSession` 音频输入输出、实时 20ms 帧和可暂停的内存播放。延迟从最后一个非静音 RMS 音频窗口结束到固定回复的首个内存播放帧；分别保留 TTS 文件文本块边界与 RMS 边界。它不包含真正 LLM／TTS、浏览器播放或实体设备。私有模式另记真实识别文本、去标点精确匹配及 CER，不以有文字输出代替识别准确。

公开 `aec_warmup_duration=0.0` 与当前生产一致：首声后约 0.5s 注入重叠，检查早期输入及暂停；物理外放回声消除仍须实体设备验收。VAD 的 `speech_duration` 包含 Silero 停声确认时间，短裁片出现暂停只能作为声学观察，不能直接判定中文意图误打断。

生产对应环境可按以下命令运行，不启动 Worker、不更改 systemd：

```powershell
wsl.exe --cd /opt/xiaoya/agent --exec /opt/xiaoya/agent/.venv/bin/python -X utf8 /mnt/c/kong/personal-projects/小芽/代码/xiaoya/scripts/benchmark_turns.py --private-stt --repeats 1 --warmups 1 --env-file /opt/xiaoya/agent/.env.local --output /mnt/c/kong/personal-projects/小芽/代码/xiaoya/.tools/turn-benchmark/private-wsl-new-run
```

报告仅写入被忽略的 `.tools/turn-benchmark/`：`run-plan.json`、每完成并关闭一个场景便刷新的 `rows.jsonl` 和 `progress.json`，全部完成后才生成 `summary.json`。取消会保留已完成记录并标记中断，不制造完整汇总。退出码 `0` 表示存在完整声学筛查候选，`1` 表示没有候选，`2` 表示运行或配置失败；候选出现不等于识别、浏览器或生产最优验收通过。已有记录的输出目录在模型初始化前拒绝复用，避免覆盖原始失败；上述 `private-wsl-new-run` 也应按每次运行换成新目录名。

2026-10-09 的私有 WSL 单重复已完成 204 个场景和 6 个暖机，STT／VAD／EOT 流分别 `210／420／210` 个且全部关闭，没有 SDK 错误或纯噪声暂停。`0.90s` 两组通过声学筛查，固定回复 p95 约 `1209ms`，组间不足 `1ms`，没有调整生产参数。真实识别精确匹配 `96／126`，CER p95 `0.0769`，识别门未通过。报告为 `private-wsl-20261009/`。

报告完成后追加了“已有输出目录拒绝覆盖”的运行前保护及回归测试；当前源码 SHA 因此不同于采样时版本。该保护不改变测量、样本、阈值或统计，原报告未重写，不能据此声称已用新 SHA 重新采样。

先前 `offline-20261009/`、`overlap-repaired-20261009/` 使用 SDK 默认 3s AEC 窗口，原始失败保留，标记为启动预热观察；部分早期裁片有 TTS 前导静音，不能与当前零预热、实际 RMS 边界的 Linux 数据直接比较。`steady-probes-20261009/` 按更新范围在 20 项后取消，保留中断状态，不计为完整验收。
