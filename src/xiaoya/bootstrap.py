"""唯一装配根：把运行时上下文连接到与框架无关的用例。"""

from livekit.agents import JobContext, JobProcess
from livekit.local_inference import init_eot
from livekit.plugins import silero

from xiaoya.application.assistant_tools import AssistantTools
from xiaoya.application.demo_catalog import DemoCatalog
from xiaoya.application.start_conversation import StartVoiceConversation
from xiaoya.domain.assistant import AssistantProfile
from xiaoya.domain.demo_catalog import DemoTicket, KnowledgeArticle
from xiaoya.infrastructure.assistant_tools import AssistantToolAdapter, SystemClock
from xiaoya.infrastructure.demo_mcp import create_demo_mcp
from xiaoya.infrastructure.livekit_conversation import LiveKitVoiceConversation
from xiaoya.infrastructure.mcp_settings import load_mcp_settings
from xiaoya.infrastructure.mcp_tools import MCPTools
from xiaoya.infrastructure.settings import Settings, load_agent_name, validate_credentials


def prewarm(process: JobProcess) -> None:
    """提前加载本地模型；用 0.30 秒静音触发批量识别，失败阻止接单而非降级。"""
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
    """每个 Job 独立装配便签和 MCP；人物在浏览器渲染，语音资源统一随 Job 回收。"""
    settings = Settings.from_environment()
    profile = AssistantProfile(instructions=settings.instructions, greeting=settings.greeting)
    conversation = LiveKitVoiceConversation(
        room=context.room,
        settings=settings,
        vad=context.proc.userdata["vad"],
        tools=AssistantToolAdapter(AssistantTools(clock=SystemClock())),
        mcp=MCPTools(settings.mcp_config_file),
    )
    context.add_shutdown_callback(conversation.close)
    return StartVoiceConversation(conversation=conversation, profile=profile)


def prepare_demo_mcp(*, host: str = "127.0.0.1", port: int = 8004):
    """演示目录在装配根注入；不依赖模型服务配置，也不读取用户的私有数据。"""
    catalog = DemoCatalog(
        articles=(
            KnowledgeArticle(
                "小芽的工具",
                "可查时间、做四则运算、保存、查看和删除本次通话便签。",
                ("工具", "能力", "计算", "时间"),
            ),
            KnowledgeArticle(
                "会话便签",
                "便签只在当前通话有效；同标题更新，通话结束后清空，不会创建提醒。",
                ("便签", "记住", "记录", "提醒"),
            ),
            KnowledgeArticle(
                "私有部署",
                "LiveKit、识别、对话和合成均使用显式配置的私有服务，不回退公共接口。",
                ("私有", "部署", "隐私", "模型"),
            ),
            KnowledgeArticle(
                "MCP 接入",
                "支持 stdio、Streamable HTTP 和 SSE；stdio 可按通话启动独立示例进程。",
                ("mcp", "协议", "接入", "知识库"),
            ),
        ),
        tickets=(
            DemoTicket(
                "DEMO-001",
                "语音助手接入咨询",
                "已受理",
                "演示客服已记录需求，等待确认私有服务地址。",
            ),
            DemoTicket("DEMO-002", "便签使用咨询", "已完成", "已提供仅在本次通话有效的便签说明。"),
            DemoTicket(
                "DEMO-003", "MCP 接入咨询", "处理中", "正在核对演示 MCP 的工具清单与接口契约。"
            ),
        ),
    )
    return create_demo_mcp(catalog, host=host, port=port)
