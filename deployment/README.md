# 本机语音服务与 DeepSeek 部署

2026-10-06 项目标识和 WSL 运行实例已迁移为 `xiaoya`，本文的部署命令与服务配置使用新名称。Agent 的 `VOICE_AGENT_AGENT_NAME` 与网页的 `AGENT_NAME` 均为 `xiaoya`；历史验收保留当时的名称。

部署目录为 WSL Ubuntu-22.04 的 `/opt/xiaoya`。Windows 工作区保留代码和网页，Linux 的依赖、模型和 Agent 独立安装，避免覆盖 Windows `.venv`。

移动 Windows 工作区后需要重新创建根目录 `.venv` 并执行 `uv sync --locked`，避免可编辑安装和激活脚本引用旧目录。语音服务使用 WSL 内的独立环境；旧 Windows `services/speech/.venv` 已归档到 `.tools/archives/rename-check-20261006-221807/`，本机代码检查使用根目录解释器。

当前小芽由浏览器 Live2D 渲染，房间仅传输助手音频，详见 [现行使用说明](../docs/live2d/README.md)。下方按日期保留的旧人物视频与语音性能记录属于对应版本的历史证据；当前验收以 [Live2D 记录](../docs/live2d/acceptance.md) 为准。

| 服务     | 本机地址                                           | 实现                                                 |
| -------- | -------------------------------------------------- | ---------------------------------------------------- |
| 网页     | http://localhost:3000                              | 官方 agent-starter-react，使用私有令牌接口           |
| LiveKit  | ws://WSL-eth0地址:7880                             | LiveKit Server 1.13.7                                |
| LLM      | https://api.deepseek.com                           | DeepSeek `deepseek-flash`，Chat Completions SSE，关闭深度思考 |
| STT      | ws://localhost:8001/v1/audio/transcriptions/stream | FunASR Paraformer 在线模型，ONNX int8，CPU           |
| TTS      | http://localhost:8001/v1/audio/speech              | CosyVoice3 0.5B，vLLM 语音 token 解码，CUDA 流式 PCM |
| 轮次检测 | Agent 进程内，无独立端口                           | LiveKit 音频 `v1-mini` + Silero VAD，CPU             |

CosyVoice 使用 RTX 5080，FunASR 在线识别、Silero VAD 和 v1-mini 使用 CPU。2026-10-07 对话模型切换为 DeepSeek，`xiaoya-ollama` 停止并禁用自启动，模型文件保留供手工回滚。语音推理保持本地运行，DeepSeek 接收识别文本、对话历史和工具上下文，不接收原始麦克风音频。首次安装需要联网下载软件与语音权重。

`SPEECH_STT_PATH=/opt/xiaoya/models/paraformer-streaming`、`SPEECH_TTS_PATH=/opt/xiaoya/models/cosyvoice3-0.5b` 明确指定权重目录。CosyVoice 源码固定到提交 `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`，包含固定的 Matcha-TTS 子模块；PyTorch 2.8.0 使用 CUDA 12.8 轮子以支持 RTX 5080。语音服务在接单前完成在线识别冲刷与真实合成预热，GPU 加载或生成失败不会回退 CPU。服务使用 uv 锁定的国内 PyPI 镜像下载依赖。

`SPEECH_TTS_ACCELERATION=vllm` 启用上游验证过的 vLLM 0.11.0 / Transformers 4.57.1 组合，语音 token 解码使用 vLLM 的 CUDA 图；声学模型仍使用 PyTorch FP16。导出的解码权重位于模型目录下的 `vllm/`，由已有本地权重生成。KV 缓存显式限制为 512 MiB，只同时解码一条请求；模型原有的 32768-token 上下文保留。该模式不支持双向文本流，仍按句输入、流式输出音频。vLLM 使用统计已关闭。回滚到原生 GPU 推理可设置 `SPEECH_TTS_ACCELERATION=none`，加速启动失败不会自行回退。

本机默认音色使用 CosyVoice 官方 `asset/zero_shot_prompt.wav` 样例。`SPEECH_TTS_PROMPT_PATH` 和 `SPEECH_TTS_PROMPT_TEXT` 可以换成自己的合法本地参考音频及对应文本；启动时缓存音色特征，之后无需每次提取。模型和参考音色不上传外部服务。

语音服务固定使用 Python 3.12，识别运行库及其原生库显式锁定为 `sherpa-onnx/sherpa-onnx-core 1.12.40`。CosyVoice 上游初始化会导入 Matcha 的配置与训练工具，因此这些必要导入依赖也由 uv 锁定；`setuptools 79.0.1` 兼容 vLLM 的版本约束并保留上游仍需的 `pkg_resources`。保留的 `openai-whisper` 包只用于参考音色的 log-mel 特征提取，ASR 不加载 Whisper 权重。

`prepare_normalizer.py` 在安装阶段下载并校验中英文规范化规则；`SPEECH_TTS_NORMALIZER_PATH=/opt/xiaoya/models/wetext` 指定本地位置。`prepare_cosyvoice.py` 为固定上游源码补充这个入口，防止运行时联网或因下载失败而忽略数字规范化。每句合成前恢复上游训练使用的 25-token 起始块长，避免预热和上一句逐步增长的块长拖慢下一句首音频；GPU 推理的 CPU 调度线程限制为 4。

当前 Silero `min_silence_duration=0.30` 秒，`TurnHandlingOptions.endpointing` 为 `min_delay=0.90`、`max_delay=1.10` 秒。VAD 静音决定何时冲刷在线识别缓存，最短轮次等待为句中停顿留出续说时间，最大等待限制 v1-mini 不确定时的延迟；两项轮次时间从最后发声开始计，不是额外串行等待。`preemptive_generation={enabled: true, preemptive_tts: true}` 让 LLM 与 TTS 都提前计算，播放仍由 SDK 在轮次确认后放行，续说时丢弃预生成。中文模型阈值保持 SDK 的 `0.355`。调整位置分别为 `src/xiaoya/bootstrap.py` 和 `src/xiaoya/infrastructure/livekit_conversation.py`。

加速服务启动时需要导出权重、初始化引擎、捕获 CUDA 图并完成真实合成预热；当前本机启动约需一分半，不计入已预热会话的首声延迟。`start-wsl.ps1` 等语音健康检查通过、DeepSeek 鉴权与非思考正文检查成功后才启动 Agent；语音健康检查最长等待五分钟，对话接口检查最长等待三分钟。服务退出会显式关闭 vLLM 子进程，避免遗留 GPU 资源。

轮次检测显式固定 `v1-mini`，不根据运行模式选择云端 `v1`；中文使用 SDK 自带的校准阈值。模型数据内置于 `livekit-local-inference` 的原生二进制，WSL 当前安装文件约 34 MB；SDK 注释将模型初始化规模标为约 108 MB，这不是独立权重文件的磁盘大小，官方未公布准确参数量。模型文件随依赖安装，无需单独下载或配置模型目录，当前接口不支持自定义独立权重路径。它在各 Agent 工作进程预热，无需另起模型服务或分配 GPU；用户打断仍由本地 Silero VAD 判断。首次启动的模型加载失败会阻止该工作进程接单。

## 配置与启动

当前本机 `.env.local`、`web/.env.local` 和 `.tools/livekit.yaml` 已生成一致的随机 LiveKit 凭据，均被版本管理忽略。不要把这些文件里的密钥粘贴到日志或文档。语音服务实现位于 `services/speech/`，单独使用 uv 锁定依赖。

DeepSeek 配置为 `VOICE_AGENT_LLM_BASE_URL=https://api.deepseek.com`、`VOICE_AGENT_LLM_MODEL=deepseek-flash` 和独立的 `VOICE_AGENT_LLM_API_KEY`。Windows 与 `/opt/xiaoya/agent/.env.local` 保持一致，Linux 配置权限为 600。当前 Agent 以 WSL 前台进程运行，Windows 隐藏启动器 PID 记录在 `.tools/logs/agent-deepseek-launcher.pid`，日志为 `agent-deepseek.out.log` 与 `agent-deepseek.err.log`；systemd Agent 服务仍为手工启动模式。

Linux 服务器可通过 systemd 管理全部后端：

```powershell
wsl -d Ubuntu-22.04 -- systemctl start xiaoya-livekit xiaoya-speech xiaoya-agent
wsl -d Ubuntu-22.04 -- systemctl is-active xiaoya-livekit xiaoya-speech xiaoya-agent
wsl -d Ubuntu-22.04 -- journalctl -u xiaoya-agent -n 50 --no-pager
```

systemd 服务本身不会让 WSL 始终保持运行。本机使用实际 Agent 的前台进程维持 WSL 生命周期，在一个保留打开的 PowerShell 终端执行：

```powershell
pwsh -File deployment/start-wsl.ps1
```

这条命令会启动 LiveKit 与本地语音两个基础服务，再检查 DeepSeek 接口并运行 Agent；关闭该终端会结束前台 Agent。Linux 服务器没有 WSL 生命周期问题，可直接使用上面的 systemd Agent 服务。网页在 Windows 上运行，在另一个终端的项目根目录执行：

```powershell
pwsh -File deployment/start-web.ps1
```

使用本地开发网页服务，令牌接口沿用官方示例的开发模式；当前网页只监听 Windows 的 127.0.0.1。对外提供服务前，应接入自己的用户鉴权并以生产模式部署。

修改 Agent 代码前，先在另一个保留的终端执行 `wsl -d Ubuntu-22.04 -- bash -lc 'exec tail -f /dev/null'` 作为临时保活。最后一个前台进程退出可能让整个 WSL 自动停止，因此保活建立后才在旧 Agent 终端按 Ctrl+C，执行精确同步命令，再重新运行 `deployment/start-wsl.ps1`。新 Agent 就绪后才结束临时保活终端：

```powershell
wsl -d Ubuntu-22.04 -- bash /mnt/c/dev/rust/xiaoya/deployment/wsl/sync-agent.sh
```

首次完整部署的 `deployment/wsl/deploy.sh` 要求 LiveKit 和 Ollama 二进制已安装到上述目录。它保留其他 WSL 服务，模型不存在时才下载，使用 SDK 的现代 Python runner 启动 Agent。完整部署也复用 `sync-agent.sh` 精确移除已退休的视频模块和 PNG，重复部署不会重新引入旧人物链路。日常 Agent 更新使用上述 `sync-agent.sh`，不重新下载模型或管理基础服务。GPU 使用情况可查看 `wsl -d Ubuntu-22.04 -- /opt/xiaoya/ollama/bin/ollama ps`。

## 停止

本机先在 Agent 前台终端按 Ctrl+C，再停止基础服务；Linux 服务器的 systemd Agent 可通过下面的命令一起停止。

```powershell
wsl -d Ubuntu-22.04 -- systemctl stop xiaoya-agent xiaoya-speech xiaoya-livekit
```

停止网页时，先读取 `.tools/logs/web.pid` 并核对该 PID 的命令行包含此项目的 Next.js 入口，再停止该进程；日志位于 `.tools/logs/`。

WSL 使用 NAT 网络。Windows 浏览器直接通过 WSL 的 eth0 地址连接信令和 WebRTC 媒体，避免本机 7880 端口的 localhost 转发问题。`start-web.ps1` 会读取当前地址并更新 `web/.env.local`；WSL 重启后，应同时重新启动网页以更新地址。移动设备或其他电脑访问需要另行配置 HTTPS、媒体路由和访问鉴权。

模型与上游来源：[FunASR 在线模型及 ONNX 转换](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-paraformer/paraformer-models.html)、[CosyVoice](https://github.com/QwenAudio/CosyVoice)、[CosyVoice3 权重](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)、[DeepSeek 接口](https://api-docs.deepseek.com/)、[官方网页示例](https://github.com/livekit-examples/agent-starter-react)。下方 Qwen 性能数据属于历史版本，不代表 DeepSeek 的当前延迟。

## DeepSeek 切换验收（2026-10-07）

Windows 与 WSL 已使用 `deepseek-flash` 的 Chat Completions SSE，语音模式关闭深度思考。WSL Agent 已重新注册；`xiaoya-ollama` 为 `inactive/disabled`，11434 端口与原 Qwen GPU 进程已退出，LiveKit 和本地语音服务继续运行。启动及完整部署脚本不再拉起 Ollama。

真实 DeepSeek 的七类内置/MCP 工具完成调用、结果回传与最终回复闭环。已部署 Agent 的真实私有 LiveKit 房间完成四轮合成语音和三轮文字用例，验证音频上行、识别、DeepSeek 回复及非零音频下行；输入是合成测试音频，未验收真人麦克风、扬声器或浏览器设备。

证据位于 `.tools/logs/deepseek-tool-verification.json` 与 `deepseek-room-verification.json`。213 项离线测试、CLI 帮助、部署脚本语法和本次修改文件的 Ruff 检查通过。全库检查仍有其他文件的 8 处既有行长问题、2 个文件的格式问题，因此不宣称全库 Ruff 通过。

## 首声优化验收（2026-10-06）

正式服务已启用 vLLM 语音 token 解码、提前合成和 0.90～1.10 秒轮次等待。声学模型保持原来的 10 步迭代、25-token 首块、FP16 与参考音色；中文分句保留原来的句内逗号和数字规则。未采用实验中不兼容的声学 CUDA 图或动态编译配置。

五轮真实私有房间测试全部完成并纳入统计。以下均为中位数；各阶段会重叠，累计节点不能直接相加。

| 阶段                   |  优化前 |  优化后 |
| ---------------------- | ------: | ------: |
| 说完到最终识别文本     |  373 ms |  388 ms |
| 说完到 LLM 首字        |  416 ms |  434 ms |
| 说完到轮次确认         | 1202 ms |  902 ms |
| TTS 请求到首块 PCM     | 1848 ms |  533 ms |
| 说完到服务端首块 PCM   | 3005 ms | 1009 ms |
| 说完到客户端首个有声帧 | 3248 ms | 1439 ms |

优化后客户端首声范围 1279～1469 ms，首声中位数缩短约 56%，TTS 首包缩短约 71%。首块 PCM 可以在轮次确认前到达，SDK 会在确认后才播放，因此 PCM 到客户端有声帧的时间也包含剩余播放等待。LLM 请求到首字约 44 ms，v1-mini 推理约 12 ms。

三轮插入 0.60～0.80 秒句中停顿，均只提交一条完整用户轮次，没有提前出声。正式 Agent 另完成两轮语音回复，以及长回复打断后的继续对话；上下行音频与角色视频均正常。114 项离线测试、Ruff、CLI 帮助、部署脚本语法及已部署文件指纹检查通过。

最新数据为 `.tools/logs/streaming-voice-latency-summary.json` 和 `streaming-voice-latency-measurement.json`；优化前记录保存在 `.tools/logs/before-fast-cosyvoice/`。停顿结果为 `streaming-voice-pause-verification.json`，正式链路为 `fast-room/room-verification.json`。测试使用固定合成文案与 WSL RTC 客户端，未验证用户真实麦克风与扬声器，也不代表并发性能。计时入口已关闭，正式入口日志为 `.tools/logs/agent-fast-final.log`，PID 记录仍使用 `agent-streaming-linux.pid`。

## FunASR / CosyVoice 首次适配记录（2026-10-06，优化前）

新服务已在 WSL 运行，正式 `voice-demo` Agent 已重新注册。使用合成语音在真实私有 LiveKit 房间完成两轮加法对话、音频回传，以及长回复中打断后继续回复；原数字人的音频与视频轨道正常。五轮识别全部正确，用户说话期间每轮出现 2～3 次临时转写，最终确认使用增量缓存。

三轮问题分别插入 0.60～0.80 秒句中停顿，均合为完整的一轮，续句开始前没有提前回复音频。真实 HTTP PCM 测试首块 2.29 秒、完整合成 6.78 秒、音频时长 7.92 秒，分 7 个 HTTP 数据块送达；合成示例保存在 `.tools/logs/cosyvoice-streaming-demo.wav`，仅包含预设测试文案。

五轮热模型延迟用单调时钟测量；其中一轮在停止说话到首声之间出现 WSL 系统时钟跳变，未纳入以下统计，剩余四轮：

| 阶段                | 中位耗时 | 计时起点                 |
| ------------------- | -------- | ------------------------ |
| VAD 确认静音        | 320 ms   | 最后发声                 |
| FunASR 冲刷最终文字 | 53 ms    | VAD 确认静音             |
| v1-mini 推理        | 13 ms    | 模型调用，和其他阶段重叠 |
| Qwen 首字           | 39 ms    | LLM 请求开始             |
| 模型首字到达        | 416 ms   | 最后发声                 |
| 轮次最终确认        | 1.20 s   | 最后发声                 |
| CosyVoice 首块 PCM  | 1.85 s   | TTS 请求开始             |
| 客户端首个有声帧    | 3.25 s   | 最后发声                 |

客户端首声范围为 3.12～3.47 秒。当前原生 PyTorch 推理的 CosyVoice 首声比此前 Piper 的 1.38 秒慢，流式输出不能消除生成首块音频的成本；ASR 的等待已经减少，TTS 是当前主要瓶颈。各阶段存在重叠，不能把表中数字直接相加。此结果为单会话合成测试，不代表并发性能或物理设备延迟。

108 项离线测试、Ruff、CLI 帮助和部署脚本语法检查通过。旧 `/opt/voice-demo/models/whisper-small` 与 Piper 的 `zh_CN-huayan-medium.onnx/.onnx.json` 已删除，uv 环境已移除 `faster-whisper`、`piper-tts` 和 `ctranslate2`。当前合成长句的 CUDA 峰值分配约 4.9 GiB，与 Qwen 和桌面 GPU 占用合计约 12.6 GiB，可在本机 16 GiB 显存运行。

最后同步独立符号过滤规则后，正式 Agent 再次重启，两轮真实房间测试再次正确回答北京和 4。16 个运行文件的 SHA-256 与最终工作区一致，Windows 与 WSL 的语音协议配置均已核对；计时测试 Agent 已结束，只保留正式入口。当前 Agent 日志为 `.tools/logs/agent-streaming-final.log`，Linux PID 记录在 `agent-streaming-linux.pid`；重启前仍须核实进程命令和目录。

结果保存在被忽略的 `.tools/logs/streaming-room/room-verification.json`、`streaming-final-production-room.json`、`streaming-voice-latency-measurement.json` 和 `streaming-voice-latency-summary.json`。本次使用合成音频和 WSL RTC 客户端，未采集或验证用户真实麦克风、扬声器。TTS 当前按中文句子输入文本并流式返回音频，尚未使用双向文本流，语速固定为 `speed=1.0`。

以下 2026-10-05 的 Whisper/Piper 结果属于替换前的历史记录，不能作为当前 FunASR/CosyVoice 的性能与验收结果。

## 已验证结果（2026-10-05）

- Ubuntu-22.04 中三个基础服务正常运行，Agent 已注册到私有 LiveKit。
- Windows Edge + Playwright 使用虚拟麦克风注入 Piper 合成的中文测试音频，完成“开始通话 → 开场白 → 询问中国首都 → Whisper 转写 → Qwen 回复北京 → Piper 合成回传 → 挂断恢复首页”。真实模型没有被模拟。
- 最后一次浏览器通话的音频上行 41,489 字节、下行 77,762 字节。页面标题、非空界面、无框架错误覆盖层、通话与挂断均通过；1440×900 通话界面和 390×844 首页已检查。
- 浏览器无运行时错误；新浏览器配置首次使用时出现两条缺少 `lk-user-choices` 本地偏好的提示，不影响通话。
- 66 项 Python 测试、Ruff、TypeScript 检查和 Next.js 生产构建通过。官方模板有两条现有 lint 警告（未使用的错误变量和多余 useMemo 依赖）。

测试使用合成音频，没有采集用户真实麦克风；真实设备音量、回声、打断和连续多轮对话仍需在浏览器中实际试听。当前部署用于本机开发，尚未配置外网访问与生产用户鉴权。

## v1-mini 接入验收（2026-10-05）

- WSL `/opt/voice-demo/agent` 已同步本地音频轮次模型配置并重启 Agent，注册到私有 LiveKit；运行文件与 Windows 源码的 SHA-256 一致。模型显式固定 `v1-mini`，打断固定为本地 VAD，进程启动时加载模型。
- 67 项 Python 测试、Ruff 检查与格式检查、CLI 帮助通过。真实 SDK 测试在伪造云推理环境变量的条件下仍使用本地 mini，并获得带真实推理耗时的预测结果；WSL 部署脚本通过 Bash 语法检查。
- WSL 原生模型对单次静音样本推理约 20.12 毫秒，此值不代表完整语音回复延迟或中文轮次检测准确率。
- 通过 LiveKit Python 客户端注入 Piper 合成语音，在真实私有房间完成两轮对话，分别得到“北京”和“2 加 2 等於 4”。语音上行 190,589 个 48 kHz 采样，音频下行 228,800 个 16 kHz 采样，其中非零采样 116,698 个。STT、LLM、TTS 和 LiveKit 均为真实本机服务。
- 验收结果保存在被忽略的 `.tools/logs/v1-mini-room-verification.json`。本次没有重新进行浏览器交互或真人设备验收；网页 HTTP 200，服务继续运行。测试删除房间时出现 SDK 会话结束事件发送警告，不影响已完成的音频收发；未观察到本地轮次模型推理错误。

本次 Agent 使用隐藏的 PowerShell 启动器维持 WSL 前台进程，启动器 PID 记录在 `.tools/logs/agent-v1-mini-launcher.pid`，日志位于 `.tools/logs/agent-v1-mini.stdout.log` 和 `.tools/logs/agent-v1-mini.stderr.log`。重启时先核对本项目 Agent 的 Linux PID 并正常结束，再执行 `deployment/start-wsl.ps1`，避免同时注册两个 Agent。

## GPU STT 与轮次等待验收（2026-10-05）

- Whisper 已切换 RTX 5080 CUDA float16，服务启动前完成有声样本预热；运行进程实际加载 CUDA 和 CUDA 12.8 cuBLAS，HTTP 健康检查正常。三个修改的 Python 源文件与 WSL 运行文件 SHA-256 一致，未改 Python 依赖。
- 最终 VAD 静音为 0.30 秒，轮次等待为 1.10～1.20 秒。较短的轮次最小等待在句中停顿测试中出现提前接话，因此最终设置为续句保留时间；中文阈值保持 0.355。
- 同样五个中文提问，从 VAD 回溯的最后发声算起，模型首字中位数由 1.31 秒降至 0.53 秒，客户端首个有声帧由 2.03 秒降至 1.38 秒，首音频中位数缩短约 32%。最终五轮首音频范围为 1.30～1.50 秒；HTTP STT 请求中位数由 680 毫秒降至 149 毫秒。各阶段存在重叠，不能直接相加。
- 三轮提问插入 0.60～0.80 秒静音，VAD 测得的实际声学间隔约 0.71、1.01、0.91 秒；均合为一轮，完整问题结束前没有回复音频，分别正确回答北京、4 和 7 天。
- 正式 `voice-demo` Agent 已重启并重新注册，两轮真实私有房间测试再次得到北京和 4，并接收到非零音频。87 项 Python 测试、Ruff 检查与格式检查、CLI 帮助及部署脚本 Bash 语法检查通过。
- 计时和停顿验证使用合成语音与 WSL Python RTC 客户端；本次未重新验收浏览器或用户真实麦克风、扬声器。五轮样本不代表并发负载下的延迟分布。识别仍偶尔将“首都”误为“手都”，GPU 提速不代表中文识别准确率已全面验证。

当前启动器 PID 记录于 `.tools/logs/agent-gpu-latency-launcher.pid`，日志为 `.tools/logs/agent-gpu-latency.stdout.log` 和 `.tools/logs/agent-gpu-latency.stderr.log`。前后计时、最终配置与停顿结果保存在 `.tools/logs/gpu-latency-comparison.json`，GPU 库加载及源码指纹保存在 `.tools/logs/gpu-latency-runtime.json`。部署前文件备份位于 WSL `/opt/voice-demo/archives/before-gpu-latency-1791212686`；计时测试的独立 Worker 已停止，正式服务继续运行。

## 小芽 Live2D（当前实现，2026-10-06）

人物由浏览器使用官方 Cubism Web SDK 与 MotionSync CRI 渲染，口型读取助手实际播放的音轨。房间只发布一条助手音轨，不再发布人物视频；Python wheel 不打包旧人物 PNG，Pillow 仅供开发工具使用。模型、动画或分析故障使用静态后备和重试，不阻塞语音与文字。

使用方法、模型源文件、固定 SDK、真实房间与浏览器复验方法见 [Live2D 说明](../docs/live2d/README.md)，分层验收见 [验收记录](../docs/live2d/acceptance.md)。角色模型随 `web/public/avatar/` 发布，Agent 仍按上面的精确同步方法更新。旧图片视频方案及本页前面的历史验收保留作对照，其中“人物视频正常”的旧结论不适用于当前实现；历史详情见 [avatar.md](avatar.md)。
