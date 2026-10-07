#!/usr/bin/env bash
# WSL 可能同时存在 DNS 虚拟网卡，媒体候选必须发布 Windows 能访问的 eth0 地址。
set -euo pipefail
node_address="$(ip -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1)"
test -n "$node_address"
exec /opt/xiaoya/bin/livekit-server --config /opt/xiaoya/livekit.yaml \
     --node-ip "$node_address"
