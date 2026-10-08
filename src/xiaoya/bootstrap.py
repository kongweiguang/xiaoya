"""唯一装配根：把运行时上下文连接到与框架无关的用例。"""

from livekit.agents import AgentServer, JobContext, JobProcess
from livekit.agents.worker import ServerEnvOption
from livekit.local_inference import init_eot
from livekit.plugins import silero

from xiaoya.application.assistant_tools import AssistantTools
from xiaoya.application.start_conversation import StartVoiceConversation
from xiaoya.domain.assistant import AssistantProfile
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter, SystemClock
from xiaoya.infrastructure.livekit_conversation import LiveKitVoiceConversation
from xiaoya.infrastructure.mcp_settings import load_mcp_settings
from xiaoya.infrastructure.mcp_tools import MCPTools
from xiaoya.infrastructure.settings import Settings, load_agent_name, validate_credentials
from xiaoya.infrastructure.systemd import notify_worker_registered
from xiaoya.infrastructure.worker_resources import configure_worker_cpu_budget


def prepare_server() -> AgentServer:
    """容量按实际部署预算计算，但预热池不随核数膨胀；开发模式仍按需加载。"""
    configure_worker_cpu_budget()
    server = AgentServer(
        setup_fnc=prewarm,
        num_idle_processes=ServerEnvOption(dev_default=0, prod_default=2),
        host="127.0.0.1",
    )
    server.on("worker_registered", notify_worker_registered)
    return server


def prewarm(process: JobProcess) -> None:
    """工作进程提前加载本地轮次模型与 VAD，0.30 秒静音为检测边界；失败阻止接单。"""
    init_eot()
    process.userdata["vad"] = silero.VAD.load(min_silence_duration=0.30)


def agent_name() -> str:
    """入口通过装配根获取派发名称，尚未配置模型时也能查看命令帮助。"""
    return load_agent_name()


def validate_configuration(*, require_livekit: bool = False) -> None:
    """启动前校验模型与 MCP 文件；房间凭据避免 CLI 选择默认云项目，预检不连接网络。"""
    settings = Settings.from_environment()
    load_mcp_settings(settings.mcp_config_file)
    if require_livekit:
        validate_credentials()


def prepare_conversation(context: JobContext) -> StartVoiceConversation:
    """工具按 Job 隔离；运行态会话结束通知 Job，启动失败仍按原异常传播。"""
    settings = Settings.from_environment()
    profile = AssistantProfile(instructions=settings.instructions, greeting=settings.greeting)
    conversation = LiveKitVoiceConversation(
        room=context.room,
        settings=settings,
        vad=context.proc.userdata["vad"],
        tools=AssistantToolAdapter(AssistantTools(clock=SystemClock())),
        mcp=MCPTools(settings.mcp_config_file),
        on_terminal=context.shutdown,
    )
    context.add_shutdown_callback(conversation.close)
    return StartVoiceConversation(conversation=conversation, profile=profile)
