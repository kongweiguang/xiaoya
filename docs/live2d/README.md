# 小芽 Live2D 使用与维护

小芽由浏览器中的官方 Cubism Web SDK 直接绘制，口型分析读取助手实际播放的远端音轨。Agent 房间只发布声音，不再发布人物视频；人物和语音服务各自拥有资源，动画异常不会结束聊天。

## 使用

启动本机私有服务和网页的方法见 [部署说明](../../deployment/README.md)。打开 `http://localhost:3000`，选择“开始聊天”并允许麦克风，或选择“用文字聊聊”。设备菜单切换输入设备；麦克风按钮表示实际采集状态。“结束聊天”释放当前采集并回到待机。

文字 Enter 发送、Shift+Enter 换行；中文输入法确认不会发送。失败时保留草稿并提供重试。房间连接与等待助手共用 30 秒期限，连接中可以取消；取消后迟到的授权会立即停止采集，不会发布到新房间。

模型加载时显示同款静态小芽，全部资源和首帧准备好后再显示动画。加载、WebGL 或口型分析失败时，提示与“重试动画”出现在固定位置。声音被浏览器阻止时使用“开启声音”恢复，受阻期间嘴部关闭。减少动态偏好使用静态形象，语音和文字仍可使用。

桌面人物与字幕并排，手机人物区、字幕区与操作区紧凑排列，底部预留安全区域。页面不显示动作参数或分析调试选项。

## 人物和口型

| 表现 | 控制来源 |
| --- | --- |
| 待机眨眼、视线和芽叶摆动 | 本地表现运行时，随机变化间隔 |
| 倾听、思考、说话神态 | 纯状态规则，错误及重连优先于迟到的说话状态 |
| 开合、圆唇、横展和闭唇 | 同一助手音轨的 PCM 经官方 MotionSync CRI 分析 |
| 静音、声音受阻、换轨及断开时收嘴 | 播放许可、音频时钟、静音阈值和分析代次 |

角色脚底和舞台位置固定，身体不作上下呼吸位移。表情、动作和物理先更新，嘴部参数最后由音频模块独占写入，字幕和 `speaking` 状态不直接驱动嘴部。MotionSync 故障会进入音量后备；音频分析整体不可用时停止口型。页面提供恢复入口，音量后备不能计为完整口型同步验收通过。

口型时间戳使用共享 `AudioContext` 时钟，并结合公开输出时间戳或输出延迟选择当前画面。有界窗口和时间线丢弃过期结果，不追赶后台积压的嘴型。高频参数直接写入模型，不逐帧更新 React 状态。

模型源文件与运行包分别交付：

| 路径 | 内容 |
| --- | --- |
| `assets/avatar/xiaoya/` | 角色概念、分层原画、PSD、可编辑 CMO3、制作工具及验证证据 |
| `web/public/avatar/xiaoya/` | MOC3、纹理、模型入口、动作、表情、物理、MotionSync 配置和静态后备图 |
| `web/public/avatar/vendor/` | 同源固定版本 Core 与许可文本 |
| `web/lib/avatar/vendor-source/` | 固定 SDK Framework 源码及局部补丁记录 |

制作方式、Core 一致性和资产指纹见 [模型说明](../../assets/avatar/xiaoya/README.md)，SDK 版本和授权见 [SDK 记录](sdk.md)。正式 CMO3 已在官方 Cubism Editor 5.3.04 中打开、保存、关闭重开并导出 SDK 5.0 模型；头饰、肩部、头颈的连接修正也已同步到工程与运行资产。132 种实际轮廓连接和 93 种制作／官方导出几何比较通过，完整结果见 [当前验收](acceptance.md)。Editor 当前使用试用许可。

## 资源所有权

`components/app/app.tsx` 为每次尝试创建独立 Room 与音频时钟，通过公开 `webAudioMix` 配置借给 LiveKit。`RoomAudioRenderer` 是唯一播放出口；分析节点仅借用助手轨道，输出增益为零，用户麦克风不驱动小芽嘴部。

`hooks/use-conversation.ts` 管理连接、取消、错误和共享关闭任务。旧任务清理后再次检查代次，不能重置后来建立的房间。挂断关闭旧时钟并创建待机房间，因此页面保留一个待机音频时钟属于预期；页面卸载才关闭全部页面资源。

通话中的信令恢复与完整重连共用 30 秒恢复窗口，期间保留会话并显示“正在恢复连接…”，等待网络和助手重新就绪；切换恢复模式或网络先恢复都不会刷新期限。首次连接通过 `useAgent` 等待就绪，通话后的恢复读取公开的实时 `Room.state` 与 `useVoiceAssistant` 当前助手状态，避免参与者暂时移除或 SDK 历史失败标记提前结束聊天。恢复成功后继续跟随实际助手声音；房间明确进入最终 `Disconnected` 状态或恢复窗口超时才自动结束并提供重新连接操作，用户仍可随时手动结束。

`AvatarAudioBridge` 负责 AudioWorklet 与轨道监听，`MotionSyncAnalyzer` 负责重采样与分析，`Live2DRuntime` 负责模型、纹理、物理和 WebGL。挂断或换轨清空分析结果；舞台可以继续待机。卸载、减少动态和重试释放各自的 native、GPU、观察器、事件和帧循环。人物组件从不停止 SDK 管理的远端音轨。

## 检查与复验

在仓库根目录运行 Python 离线检查；这些命令不连接真实模型或房间：

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run xiaoya --help
```

在 `web` 目录运行前端检查。单独的 `.next-build` 目录避免生产构建干扰正在运行的开发缓存：

```powershell
pnpm install --frozen-lockfile
pnpm sdk:build
pnpm test
pnpm lint
pnpm format:check
pnpm exec tsc --noEmit
$env:NEXT_DIST_DIR = '.next-build'
pnpm build
Remove-Item Env:NEXT_DIST_DIR
```

真实私有房间复验由显式命令触发。当前 `verify-avatar-room.py` 验证音频上行、识别、回复、下行及播放中打断，要求一条音轨和零条视频轨；它不负责验证浏览器中的 Live2D 画面：

```powershell
wsl -d Ubuntu-22.04 -- bash -lc 'cd /opt/xiaoya/agent; .venv/bin/python /mnt/c/dev/rust/xiaoya/deployment/verify-avatar-room.py --output /mnt/c/dev/rust/xiaoya/.tools/logs/live2d-room'
```

浏览器口型复验使用不少于 30 条固定合成 WAV，覆盖短音节、插入停顿、连续中文和长回复。生成命令使用 `.env.local` 指定的私有 TTS，不保存用户音频：

```powershell
uv run python deployment/generate-avatar-utterances.py --reuse-valid
Set-Location web
pnpm avatar:verify
```

打开 `http://127.0.0.1:3001`，点击“运行 30 条口型验收”。测试使用真实运行模型、MotionSync 和音频桥接，只建立一路可听出口，测量开口时序、尾音和停顿，随后执行 20 次合成音频连接与回收。`report.json` 和带声音的 `recording.webm` 保存在 `.tools/live2d-verification/results/`。复验前重新加载页面，以隔离上一轮计数。

“开始 30 分钟稳定性”独立执行长期播放，每条语音结束后保存 `stability.json`；只有 `complete=true` 才表示达到完整时长，取消只保留部分证据。逐帧记录、PCM 队列及遥测均有容量上限。测试页应保持可见，以免后台节流影响帧率。长期测试不录像，以限制内存占用。

真实浏览器房间的反复连接与挂断另行验证，不能由上述合成轨道回收代替。独立标签只选择“用文字聊聊”，每轮确认助手就绪、MotionSync 启用和挂断恢复，检查结束后的音频元素及上下文数量。若使用临时预加载探针，完成后移除脚本并重新加载或关闭测试标签。

验收结果与分层证据由 [验收记录](acceptance.md) 统一列出，原始文件在 `deployment/evidence/live2d-2026-10-06/`。软件输出时钟测量和浏览器合成输入不能替代用户真实麦克风、扬声器、回声与物理输出延迟验收；口型形状和细小美术边缘仍需人工观看评估。公网发布、账户体系及语音模型更换不在本次范围。

## 更新部署

网页修改使用 pnpm 管理依赖并同步锁文件，SDK 和模型资源同源发布。更新模型时保留完整 `model3.json` 引用，重新检查 MOC 一致性、纹理、参数范围、静态图和资源指纹；不要只替换一张概念图。

Agent 已移除旧人物视频装配和 PNG 运行依赖。`deployment/wsl/sync-agent.sh` 精确清理退休模块与其专属运行素材目录，只更新本项目 Agent 源码和 uv 依赖，保留其他服务和语音权重。Windows 原始 PNG 与历史验收仍保留，Python wheel 不再打包旧人物素材。

重启本机 Agent 前先在另一个保留的终端运行独立 WSL 前台进程，避免结束最后一个前台进程时整个 WSL 自动退出：

```powershell
wsl -d Ubuntu-22.04 -- bash -lc 'exec tail -f /dev/null'
```

随后核实本项目 Agent 的实际 PID、命令行和目录，正常结束旧 Agent，再同步并启动；不要同时运行前台 Agent 和 systemd Agent：

```powershell
wsl -d Ubuntu-22.04 -- bash -lc 'bash /mnt/c/dev/rust/xiaoya/deployment/wsl/sync-agent.sh'
pwsh -File deployment/start-wsl.ps1
```

新 Agent 已正常运行后才结束临时保活终端。验证服务状态、源码指纹和真实音频房间，勿沿用历史 PID 或旧视频断言。历史图片视频方案记录位于 [deployment/avatar.md](../../deployment/avatar.md)，不能作为当前 Live2D 验收结论。
