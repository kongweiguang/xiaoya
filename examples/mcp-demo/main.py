"""独立 MCP 服务入口，无需 LiveKit 或私有模型即可演示标准协议。"""

import argparse

from xiaoya_mcp_demo.bootstrap import prepare_demo_mcp


def main() -> None:
    """默认 stdio 供显式配置的 Job 托管，HTTP 供已有服务场景；stdout 留给协议帧。"""
    parser = argparse.ArgumentParser(description="xiaoya 演示知识库与模拟工单 MCP 服务")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port 必须在 1 到 65535 之间")
    prepare_demo_mcp(host=args.host, port=args.port).run(transport=args.transport)


if __name__ == "__main__":
    main()
