"""无损整理 PSD 元数据与图层顺序，不解码、缩放或重绘图层像素。"""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path


def reverse_layer_records(tail: bytes) -> tuple[bytes, list[dict[str, object]]]:
    """同步倒序记录与原通道块，使当前 Cubism 导入绘制顺序符合角色遮挡关系。"""
    section_length, info_length = struct.unpack_from(">II", tail)
    signed_count = struct.unpack_from(">h", tail, 8)[0]
    cursor = 10
    descriptors = []
    for _ in range(abs(signed_count)):
        start = cursor
        top, left, bottom, right, channel_count = struct.unpack_from(">iiiiH", tail, cursor)
        cursor += 18
        lengths = []
        for _ in range(channel_count):
            _, length = struct.unpack_from(">hI", tail, cursor)
            lengths.append(length)
            cursor += 6
        if tail[cursor : cursor + 4] != b"8BIM":
            raise ValueError("未知图层混合签名，禁止猜测修复")
        extra_length = struct.unpack_from(">I", tail, cursor + 12)[0]
        cursor += 16 + extra_length
        descriptors.append((tail[start:cursor], lengths, (top, left, bottom, right)))
    records = []
    channel_audit = []
    for record, lengths, bounds in descriptors:
        total = sum(lengths)
        channels = tail[cursor : cursor + total]
        if len(channels) != total:
            raise ValueError("图层通道数据越界")
        cursor += total
        records.append((record, channels))
        channel_audit.append(
            {"bounds": bounds, "bytes": total, "sha256": hashlib.sha256(channels).hexdigest()}
        )
    info_end = 8 + info_length
    if cursor > info_end or info_end > 4 + section_length:
        raise ValueError("图层信息段长度不正确")
    # 每个通道块仅换位置；透明度、压缩方式及原字节全部原样保留。
    rebuilt = tail[:10] + b"".join(record for record, _ in reversed(records))
    rebuilt += b"".join(channels for _, channels in reversed(records))
    rebuilt += tail[cursor:]
    if len(rebuilt) != len(tail):
        raise AssertionError("图层倒序不能改变容器长度")
    return rebuilt, channel_audit


def prepare_container(
    source: Path, destination: Path, *, reverse_layers: bool = False
) -> dict[str, object]:
    """元数据与顺序准备只产生新候选，原素材、图层字节和模型工程均不覆盖。"""
    original = source.read_bytes()
    if len(original) < 38 or original[:6] != b"8BPS\0\1":
        raise ValueError("仅处理普通 PSD v1，不猜测损坏文件结构")
    color_length = struct.unpack_from(">I", original, 26)[0]
    resource_offset = 30 + color_length
    resource_length = struct.unpack_from(">I", original, resource_offset)[0]
    tail_offset = resource_offset + 4 + resource_length
    if tail_offset + 4 > len(original):
        raise ValueError("资源段越界")
    # Adobe ResolutionInfo 使用 16.16 定点 72dpi；这是显示元数据，不触碰像素。
    resolution = struct.pack(">IHHIHH", 72 << 16, 1, 1, 72 << 16, 1, 1)
    resource = b"8BIM" + struct.pack(">H", 1005) + b"\0\0"
    resource += struct.pack(">I", len(resolution)) + resolution
    existing_resources = original[resource_offset + 4 : tail_offset]
    resources = existing_resources + resource
    original_tail = original[tail_offset:]
    prepared_tail, layer_audit = (
        reverse_layer_records(original_tail) if reverse_layers else (original_tail, [])
    )
    prepared = (
        original[:resource_offset] + struct.pack(">I", len(resources)) + resources + prepared_tail
    )
    if destination.resolve() == source.resolve() or destination.exists():
        raise ValueError("候选必须使用新路径，禁止覆盖原始 PSD")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(prepared)
    if not reverse_layers and prepared[resource_offset + 4 + len(resources) :] != original_tail:
        raise AssertionError("图层／合成像素区域必须逐字节保持不变")
    return {
        "source": str(source),
        "candidate": str(destination),
        "sourceSha256": hashlib.sha256(original).hexdigest(),
        "candidateSha256": hashlib.sha256(prepared).hexdigest(),
        "pixelRegionSha256": hashlib.sha256(original[tail_offset:]).hexdigest(),
        "pixelRegionUnchanged": not reverse_layers,
        "layerOrderReversed": reverse_layers,
        "layerChannelsUnchanged": True,
        "layerChannelAudit": layer_audit,
        "addedMetadataBytes": len(prepared) - len(original),
        "officialEditorImported": False,
    }


def main() -> None:
    """准备结果只是无损容器候选，官方导入和模型制作仍需分别验证。"""
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--reverse-layers", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare_container(args.source, args.destination, reverse_layers=args.reverse_layers),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
