# 项目开发规范

> 活动工作区为 `C:\dev\rust\xiaoya`，以下规范适用于当前项目。

## 项目定位与环境

- 本项目是 Python LiveKit Voice Agent，操作系统按 Windows 11 处理。
- 统一使用 uv 管理 Python、虚拟环境、依赖和命令；Python 使用 `.python-version` 指定的 3.12。
- 普通命令优先通过 PowerShell 7.x（`pwsh`）执行；遇到兼容性问题时回退到 Windows PowerShell 5.1（`powershell.exe`），并说明回退原因。
- 添加依赖使用 `uv add`，开发依赖使用 `uv add --dev`，同步环境使用 `uv sync --locked`。
- 修改依赖时同步更新 `pyproject.toml` 和 `uv.lock`。不要手工修改锁文件，不要绕过 uv 使用 pip 安装项目依赖。

## DDD 架构约束

当前限界上下文为“语音会话”。遵循轻量 DDD：围绕业务概念和用例建模，按实际需求增加实体、聚合、仓储，不创建没有业务用途的抽象。

代码位于 `src/xiaoya/`，职责如下：

- `domain/`：领域对象和业务不变量，例如助手的对话规则、开场白。只能依赖标准库及本层代码；不得依赖 LiveKit、环境变量、网络、配置或其他层。
- `application/`：应用用例和端口（`Protocol`）。负责业务流程的顺序及失败传播，仅依赖领域层、本层和标准库；不得接触 LiveKit 的房间、会话、模型对象。
- `infrastructure/`：端口的具体实现、LiveKit SDK、模型配置和环境变量读取。通过适配器实现应用层端口，不能把业务规则藏在 SDK 回调里。
- `interfaces/`：CLI 和 LiveKit Job 入口。只处理启动、协议上下文及用例调用；不得编写业务规则，也不得直接构造基础设施实现。
- `bootstrap.py`：唯一的依赖装配入口，可以连接各层；每个 Job 创建独立的会话和用例，只在工作进程内复用 VAD 模型。

依赖方向为 `application -> domain`、`infrastructure -> application/domain`。入口通过 `bootstrap` 获取装配结果。框架对象只允许出现在外层，禁止反向导入和跨层循环依赖。

新增业务行为先定义领域规则和应用用例，再实现外部服务适配器。模型供应商的替换应发生在基础设施层。LLM 工具应委托应用用例，不得直接包含业务或数据库逻辑。

本地语音推理服务位于 `services/speech/src/local_speech/`，同样遵循轻量 DDD：`domain.py` 定义结果值对象，`application.py` 定义推理端口及用例，`infrastructure.py` 实现本地模型，`api.py` 处理 HTTP 协议，`bootstrap.py` 装配。阻塞推理在线程中执行，各模型使用独立锁限制同时推理。依赖由该服务自己的 `pyproject.toml` 与 `uv.lock` 管理。

官方网页示例位于 `web/`，使用 pnpm 管理前端依赖；不得让前端继承官网演示 Agent 或开发云令牌服务。网页固定访问本项目的服务端令牌接口，LiveKit Secret 不发送给浏览器。

WSL 部署见 `deployment/README.md`。运行时文件放入 `/opt/xiaoya`，不要在 WSL 中覆盖主项目的 Windows `.venv`。服务名使用 `xiaoya-` 前缀，只管理本项目的 systemd 服务。新增部署脚本不得停止现有无关服务。

## 编码与运行约束

- 为所有新增或修改的函数、方法及测试函数添加函数级注释（Python 使用中文 docstring），重点解释设计原因、约束和取舍，不复述函数名称。
- 异步 SDK 的调用保持异步，不在事件循环内执行阻塞 I/O。
- 会话启动成功后才发送开场白；启动失败必须向上传播，禁止吞掉异常或假装成功。
- 会话关闭由 LiveKit Job 生命周期负责触发，释放 SDK 资源；不得在入口返回时立即关闭仍在通话中的会话。
- 密钥只放入环境变量或被忽略的 `.env.local`；示例配置仅使用占位符，不记录密钥和用户音频。
- LiveKit、STT 和 TTS 使用私有化部署；LLM 按用户 2026-10-07 的指定使用 DeepSeek 官方 `deepseek-flash`。模型通过 OpenAI 兼容协议的插件适配器连接，必须显式配置每个服务的 `BASE_URL`、模型名和独立密钥，不允许配置缺失时回退其他公共 API 或 LiveKit Cloud Inference。
- `openai` 插件在本项目仅表示接口协议；不得使用公共 OpenAI 地址、供应商快捷工厂或从 `OPENAI_API_KEY` 继承密钥。没有鉴权的私有服务使用明确的 `not-required` 占位值。
- DeepSeek 使用 `https://api.deepseek.com` 的 Chat Completions SSE，语音模式显式发送 `thinking.type=disabled`，密钥只放入被忽略的 `.env.local`。WSL 的 `xiaoya-ollama` 已退出现行部署，启动和部署脚本不得自动拉起它。
- 轮次结束使用本地 Silero VAD 配合显式固定的 `inference.TurnDetector(version="v1-mini")`，打断检测使用本地 VAD；不得启用 SDK 默认的云端 TurnDetector、自适应打断检测或 Cloud 噪声处理。本地轮次模型在工作进程预热，初始化失败必须向上传播。
- 私有接口当前契约为 HTTP 转写、Chat Completions SSE、HTTP 音频合成。不同协议只在基础设施层扩展适配器，保持领域和应用端口不变。
- 启动使用官方独立 LiveKit CLI 的 `lk agent console/dev/start`，由 `xiaoya` 入口加载配置并转发；禁止新增已弃用的 `cli.run_app` 调用。服务器或容器可用 SDK 的 `python -m livekit.agents start <entrypoint>`。
- 房间模式调用 CLI 前必须检查完整的私有 LiveKit 凭据，避免从 CLI 的默认项目配置继承云服务。帮助、设备列表和模型下载不要求模型配置；本机 console 不要求 LiveKit 凭据。

## 验证与交付

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run xiaoya --help
```

测试覆盖领域不变量、用例调用顺序和失败路径、配置校验、DDD 导入边界、SDK 适配器生命周期和私有 HTTP 请求契约。默认测试不连接外部网络、真实 LiveKit 房间或模型服务；需要接口验证时使用本地内存 MockTransport。

运行真实语音对话前，复制 `.env.example` 为 `.env.local`，填写私有模型服务的地址、模型、音色和鉴权参数。安装官方 LiveKit CLI（Windows：`winget install LiveKit.LiveKitCLI`），或在项目 `.tools/lk.exe` 放置官方二进制；`.tools/` 不纳入版本管理。终端模式 `uv run xiaoya console` 不需要 LiveKit 凭据；房间模式 `uv run xiaoya dev` 使用私有 LiveKit 的 URL、API Key 和 API Secret，并由独立 CLI 托管热重载。模型准备使用 `uv run xiaoya download-files`，离线部署前预先安装依赖及准备本地模型文件。

2026-10-05 已核对官方快速入门、启动模式和 OpenAI 兼容 LLM 插件文档，当前最新稳定 SDK 为 1.8.4，独立 CLI 为 2.18.8。以后调整 SDK 或启动入口时重新核对官方文档和实际命令；不要把官方仓库尚未发布的 main 分支功能当成稳定版能力。

交付时区分离线检查通过、私有接口兼容性、完整房间语音链路以及真人设备验收。完整链路必须连接真实私有模型与 LiveKit 房间，验证音频上行、识别、模型回复及音频下行。自动验收可以向浏览器虚拟麦克风注入合成测试音频，但必须明确说明，不能声称已经验证用户的真实麦克风和扬声器。

## CodeGraph

仅当仓库根目录存在 `.codegraph/` 时，理解或定位代码应优先使用 CodeGraph：先调用 `codegraph_explore`（如可用），或执行 `codegraph explore "符号名称或问题"`，再补充文本搜索或文件读取。没有 `.codegraph/` 时跳过，不自动创建索引。
