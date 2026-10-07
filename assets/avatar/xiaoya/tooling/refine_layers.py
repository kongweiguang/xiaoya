"""仅精修原创零件的透明边界，保留已有绑定所依赖的尺寸与定位。"""

import argparse
import csv
import hashlib
import json
import shutil
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter


def dominant_component(mask: Image.Image) -> Image.Image:
    """最大连通主体提供保守轮廓，背景散点不能因颜色相近而混进零件。"""
    width, height = mask.size
    values = list(mask.get_flattened_data())
    seen = bytearray(width * height)
    largest: list[int] = []
    for index, value in enumerate(values):
        if not value or seen[index]:
            continue
        seen[index] = 1
        queue = deque([index])
        component = []
        while queue:
            current = queue.popleft()
            component.append(current)
            x, y = current % width, current // width
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if nx < 0 or nx >= width or ny < 0 or ny >= height:
                        continue
                    neighbour = ny * width + nx
                    if values[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        queue.append(neighbour)
        if len(component) > len(largest):
            largest = component
    output = Image.new("L", mask.size)
    retained = bytearray(width * height)
    for index in largest:
        retained[index] = 255
    output.frombytes(bytes(retained))
    return output


def corrupted_chroma(pixel: tuple[int, int, int, int]) -> bool:
    """原画主体为低饱和奶白与薄荷色，高亮高饱和杂色只在透明边界修复。"""
    red, green, blue, alpha = pixel
    maximum, minimum = max(red, green, blue), min(red, green, blue)
    return alpha > 0 and maximum >= 160 and (maximum - minimum) / maximum >= 0.72


def refine_layer(image: Image.Image) -> tuple[Image.Image, dict[str, int]]:
    """内部像素逐字节保留，清理限制在三像素边界且不重采样或裁剪。"""
    original = image.convert("RGBA")
    pixels = list(original.get_flattened_data())
    structural = Image.new("L", original.size)
    structural.putdata([255 if p[3] >= 128 and not corrupted_chroma(p) else 0 for p in pixels])
    body = dominant_component(structural)
    # 开运算只去掉边界的细小凸粒；内部高光、线条和空腔不参与颜色重绘。
    smooth_body = body.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    permitted = smooth_body.filter(ImageFilter.MaxFilter(3))
    interior = smooth_body.filter(ImageFilter.MinFilter(7))
    alpha_edge = smooth_body.filter(ImageFilter.GaussianBlur(0.45))
    kept = list(permitted.get_flattened_data())
    inside = list(interior.get_flattened_data())
    edge = list(alpha_edge.get_flattened_data())
    output = []
    removed = corrected = interior_changed = 0
    before_chroma = after_chroma = 0
    for index, pixel in enumerate(pixels):
        red, green, blue, alpha = pixel
        bad = corrupted_chroma(pixel)
        before_chroma += int(bad)
        if inside[index]:
            result = pixel
        elif not kept[index] or bad:
            result = (0, 0, 0, 0)
        else:
            result = (red, green, blue, min(alpha, edge[index]))
        if result != pixel:
            corrected += 1
            removed += int(alpha > 0 and result[3] == 0)
            interior_changed += int(bool(inside[index]))
        after_chroma += int(corrupted_chroma(result))
        output.append(result)
    if interior_changed or after_chroma:
        raise ValueError("精修超出边界或仍有高饱和杂色，拒绝输出。")
    refined = Image.new("RGBA", original.size)
    refined.putdata(output)
    return refined, {
        "changedPixels": corrected,
        "removedOpaquePixels": removed,
        "interiorChangedPixels": interior_changed,
        "corruptedChromaBefore": before_chroma,
        "corruptedChromaAfter": after_chroma,
    }


def main() -> None:
    """输出独立候选和证据，验证及目视检查通过后才更新正式制作资产。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.output.resolve()
    if destination == source or source in destination.parents:
        raise ValueError("候选输出必须位于制作源目录以外，避免覆盖输入。")
    layers = destination / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    raw_sheet = source / "source-sheet.original.png"
    if not raw_sheet.exists():
        raw_sheet = source / "source-sheet.png"
    source_sheet = Image.open(raw_sheet).convert("RGBA")
    sheet = Image.new("RGBA", source_sheet.size)
    report = {
        "canvas": source_sheet.size,
        "layers": [],
        "resampled": False,
        "sourceSha256": hashlib.sha256(raw_sheet.read_bytes()).hexdigest(),
    }
    with (source / "layers/extraction.tsv").open(encoding="utf-8") as manifest:
        for record in csv.DictReader(manifest, delimiter="\t"):
            name = record["id"]
            left, top = int(record["source-x"]), int(record["source-y"])
            width, height = int(record["source-width"]), int(record["source-height"])
            if (
                left < 0
                or top < 0
                or left + width > source_sheet.width
                or top + height > source_sheet.height
            ):
                raise ValueError("零件边界超出原画，拒绝补充虚构像素。")
            original = source_sheet.crop((left, top, left + width, top + height))
            refined, statistics = refine_layer(original)
            refined.save(layers / f"{name}.png")
            location = (int(record["source-x"]), int(record["source-y"]))
            sheet.paste(refined, location)
            report["layers"].append({"id": name, "size": original.size, **statistics})
    for name in ("layers.tsv", "extraction.tsv"):
        shutil.copy2(source / "layers" / name, layers / name)
    sheet.save(destination / "source-sheet.png")
    (destination / "refinement-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
