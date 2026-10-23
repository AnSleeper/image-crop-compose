"""图像处理核心：裁剪、描边、拼接排版、合成与导出。

本模块只依赖 Pillow，不依赖任何界面代码，方便单独测试。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from PIL import Image, ImageDraw

# ---------------------------------------------------------------- 常量

BORDER_NONE = "无"
BORDER_WHITE = "白色"
BORDER_BLACK = "黑色"
BORDER_OPTIONS = (BORDER_NONE, BORDER_WHITE, BORDER_BLACK)

BORDER_COLORS = {
    BORDER_WHITE: (255, 255, 255, 255),
    BORDER_BLACK: (0, 0, 0, 255),
}

STITCH_HORIZONTAL = "横向"
STITCH_VERTICAL = "纵向"

ALIGN_START = "起始"
ALIGN_CENTER = "居中"
ALIGN_END = "末端"
ALIGN_OPTIONS = (ALIGN_START, ALIGN_CENTER, ALIGN_END)

FIT_CONTAIN = "适应"
FIT_COVER = "填充"
FIT_STRETCH = "拉伸"
FIT_ORIGINAL = "原始"
FIT_OPTIONS = (FIT_CONTAIN, FIT_COVER, FIT_STRETCH, FIT_ORIGINAL)

MODE_FREE = "自由"
MODE_STITCH = "顺序拼接"
MODE_GRID = "网格"
MODE_OPTIONS = (MODE_FREE, MODE_STITCH, MODE_GRID)

TRANSPARENT = None  # 背景 / 间隙为透明时使用


# ---------------------------------------------------------------- 基础工具

def to_rgba(img: Image.Image) -> Image.Image:
    """统一转换为 RGBA，便于后续保留透明通道。"""
    return img if img.mode == "RGBA" else img.convert("RGBA")


def load_image(path: str) -> Image.Image:
    """读取图片（动图取第一帧）并转为 RGBA。"""
    img = Image.open(path)
    if getattr(img, "n_frames", 1) > 1:
        try:
            img.seek(0)
        except EOFError:
            pass
    return to_rgba(img)


def contain_size(src_w: float, src_h: float, max_w: float, max_h: float) -> tuple[int, int]:
    """等比缩放到不超过 (max_w, max_h) 的最大尺寸。"""
    if src_w <= 0 or src_h <= 0 or max_w <= 0 or max_h <= 0:
        return (1, 1)
    ratio = min(max_w / src_w, max_h / src_h)
    return (max(1, int(round(src_w * ratio))), max(1, int(round(src_h * ratio))))


def cover_crop(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """返回「等比铺满 dst」需要从源图裁掉的居中裁剪框。"""
    if src_w <= 0 or src_h <= 0:
        return (0, 0, max(1, src_w), max(1, src_h))
    ratio = max(dst_w / src_w, dst_h / src_h)
    crop_w = min(src_w, dst_w / ratio)
    crop_h = min(src_h, dst_h / ratio)
    x0 = (src_w - crop_w) / 2
    y0 = (src_h - crop_h) / 2
    return (int(round(x0)), int(round(y0)), int(round(x0 + crop_w)), int(round(y0 + crop_h)))


def normalize_box(box: Sequence[float]) -> tuple[int, int, int, int]:
    """把任意方向的框标准化为 (x0, y0, x1, y1)，且 x1>x0 / y1>y0。"""
    x0, y0, x1, y1 = box
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return (int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(x1)), int(math.ceil(y1)))


# ---------------------------------------------------------------- 裁剪

def crop_rect(img: Image.Image, box: Sequence[float]) -> Image.Image:
    """矩形裁剪。"""
    x0, y0, x1, y1 = normalize_box(box)
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError("选区太小，无法裁剪")
    return img.crop((x0, y0, x1, y1))


def crop_polygon(img: Image.Image, points: Sequence[tuple[float, float]], supersample: int = 4):
    """不规则多边形裁剪：多边形以外区域透明，边缘做抗锯齿。

    返回 (裁剪结果, 多边形在裁剪结果坐标系内的顶点)，顶点用于后续沿形状描边。
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        raise ValueError("多边形至少需要 3 个顶点")

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0 = max(0, int(math.floor(min(xs))))
    y0 = max(0, int(math.floor(min(ys))))
    x1 = min(img.width, int(math.ceil(max(xs))))
    y1 = min(img.height, int(math.ceil(max(ys))))
    w, h = x1 - x0, y1 - y0
    if w < 1 or h < 1:
        raise ValueError("多边形范围为空，无法裁剪")

    local = [(px - x0, py - y0) for px, py in pts]
    scale = max(1, int(supersample))
    mask = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(mask).polygon(
        [(px * scale, py * scale) for px, py in local], fill=255
    )
    if scale > 1:
        mask = mask.resize((w, h), Image.LANCZOS)

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img.crop((x0, y0, x1, y1)), (0, 0), mask)
    return out, local


def apply_border(img: Image.Image, border: str, width: int,
                 outline: Sequence[tuple[float, float]] | None = None) -> Image.Image:
    """给裁剪结果加白边或黑边。

    矩形裁剪（outline 为 None）向外扩一圈纯色边框；
    不规则形状（outline 为多边形顶点）沿轮廓向外描边，形状外保持透明。
    """
    if border not in BORDER_COLORS or width <= 0:
        return img
    color = BORDER_COLORS[border]

    if not outline:
        w, h = img.size
        out = Image.new("RGBA", (w + 2 * width, h + 2 * width), color)
        out.paste(img, (width, width), img)
        return out

    pad = width + 2
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    min_x = min(0, math.floor(min(xs))) - pad
    min_y = min(0, math.floor(min(ys))) - pad
    max_x = max(img.width, math.ceil(max(xs))) + pad
    max_y = max(img.height, math.ceil(max(ys))) + pad
    out_w = int(max_x - min_x)
    out_h = int(max_y - min_y)
    ox, oy = -min_x, -min_y
    path = [(x + ox, y + oy) for x, y in outline]

    # 沿轮廓画一条宽度 2*width 的线：内侧一半会被原图盖住，外侧留下 width 的边框
    scale = 4
    layer = Image.new("RGBA", (out_w * scale, out_h * scale), (0, 0, 0, 0))
    ImageDraw.Draw(layer).line(
        [(x * scale, y * scale) for x, y in path + [path[0]]],
        fill=color, width=max(1, int(round(2 * width * scale))), joint="curve",
    )
    layer = layer.resize((out_w, out_h), Image.LANCZOS)

    out = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    out.alpha_composite(layer)
    out.alpha_composite(img, (int(ox), int(oy)))
    return out


# ---------------------------------------------------------------- 粘贴与合成

def paste_clipped(base: Image.Image, img: Image.Image, x: float, y: float,
                  clip: tuple[int, int, int, int] | None = None) -> None:
    """把 img 粘贴到 base 的 (x, y)，可选裁剪到 clip 区域内（避免溢出）。"""
    if clip is not None:
        cx0, cy0, cx1, cy1 = clip
        sx0 = max(0, int(round(cx0 - x)))
        sy0 = max(0, int(round(cy0 - y)))
        sx1 = min(img.width, int(round(cx1 - x)))
        sy1 = min(img.height, int(round(cy1 - y)))
        if sx1 - sx0 < 1 or sy1 - sy0 < 1:
            return
        if (sx0, sy0, sx1, sy1) != (0, 0, img.width, img.height):
            img = img.crop((sx0, sy0, sx1, sy1))
            x += sx0
            y += sy0
    base.paste(img, (int(round(x)), int(round(y))), img)


def render_composition(size: Sequence[int], placements: Iterable[tuple], background) -> Image.Image:
    """按排版结果合成一整张图。

    placements 元素为 (img, x, y, w, h, crop_box)：
      x, y   —— 目标位置（左上角）
      w, h   —— 目标绘制尺寸
      crop_box —— 需要先从 img 上裁下来的区域（等比铺满用），None 表示不裁
    """
    width = max(1, int(size[0]))
    height = max(1, int(size[1]))
    base = Image.new("RGBA", (width, height), background or (0, 0, 0, 0))
    for img, x, y, w, h, crop_box in placements:
        src = img.crop(crop_box) if crop_box else img
        target = (max(1, int(w)), max(1, int(h)))
        if src.size != target:
            src = src.resize(target, Image.LANCZOS)
        paste_clipped(base, src, x, y)
    return base


def content_bounds(placements: Iterable[tuple]) -> tuple[int, int, int, int] | None:
    """计算排版内容的包围盒 (x0, y0, x1, y1)。"""
    boxes = [(x, y, x + w, y + h) for _, x, y, w, h, _ in placements]
    if not boxes:
        return None
    return (
        int(math.floor(min(b[0] for b in boxes))),
        int(math.floor(min(b[1] for b in boxes))),
        int(math.ceil(max(b[2] for b in boxes))),
        int(math.ceil(max(b[3] for b in boxes))),
    )


# ---------------------------------------------------------------- 自动排版

def _align_offset(size: int, total: int, align: str) -> int:
    if align == ALIGN_CENTER:
        return (total - size) // 2
    if align == ALIGN_END:
        return total - size
    return 0


def stitch_positions(sizes: Sequence[tuple[int, int]], direction: str = STITCH_HORIZONTAL,
                     spacing: int = 0, align: str = ALIGN_START):
    """顺序拼接：返回 ([(x, y), ...], 整体尺寸)。"""
    sizes = list(sizes)
    if not sizes:
        return [], (1, 1)

    spacing = max(0, int(spacing))
    positions = []
    if direction == STITCH_VERTICAL:
        total_w = max(w for w, _ in sizes)
        total_h = sum(h for _, h in sizes) + spacing * (len(sizes) - 1)
        y = 0
        for w, h in sizes:
            positions.append((_align_offset(w, total_w, align), y))
            y += h + spacing
    else:
        total_w = sum(w for w, _ in sizes) + spacing * (len(sizes) - 1)
        total_h = max(h for _, h in sizes)
        x = 0
        for w, h in sizes:
            positions.append((x, _align_offset(h, total_h, align)))
            x += w + spacing
    return positions, (total_w, total_h)


def grid_layout(sizes: Sequence[tuple[int, int]], rows: int, cols: int, gap: int,
                cell: tuple[int, int] | None = None, fit: str = FIT_CONTAIN):
    """网格排版：返回 (整体尺寸, [(x, y, w, h, crop_box), ...])，与 sizes 一一对应。"""
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    gap = max(0, int(gap))
    sizes = list(sizes)
    if not sizes:
        return (1, 1), []

    if cell is None:
        cell_w = max(w for w, _ in sizes)
        cell_h = max(h for _, h in sizes)
    else:
        cell_w, cell_h = cell
    cell_w = max(1, int(cell_w))
    cell_h = max(1, int(cell_h))

    placements = []
    for i, (sw, sh) in enumerate(sizes):
        row, col = divmod(i, cols)
        cx = col * (cell_w + gap)
        cy = row * (cell_h + gap)

        if fit == FIT_STRETCH:
            w, h, crop = cell_w, cell_h, None
        elif fit == FIT_COVER:
            w, h, crop = cell_w, cell_h, cover_crop(sw, sh, cell_w, cell_h)
        elif fit == FIT_ORIGINAL:
            vw, vh = min(sw, cell_w), min(sh, cell_h)
            ox, oy = (sw - vw) // 2, (sh - vh) // 2
            w, h, crop = vw, vh, (ox, oy, ox + vw, oy + vh)
        else:  # 适应
            w, h = contain_size(sw, sh, cell_w, cell_h)
            crop = None

        x = cx + (cell_w - w) // 2
        y = cy + (cell_h - h) // 2
        placements.append((x, y, w, h, crop))

    size = (cols * cell_w + (cols - 1) * gap, rows * cell_h + (rows - 1) * gap)
    return size, placements


# ---------------------------------------------------------------- 界面辅助

def make_checker(width: int, height: int, square: int = 12, offset: tuple[int, int] = (0, 0),
                 light: tuple[int, int, int] = (255, 255, 255),
                 dark: tuple[int, int, int] = (205, 205, 205)) -> Image.Image:
    """生成透明背景提示用的棋盘格（只生成可视区域，避免大图开销）。"""
    width = max(1, int(width))
    height = max(1, int(height))
    square = max(2, int(square))
    img = Image.new("RGB", (width, height), light)
    draw = ImageDraw.Draw(img)
    step = square * 2
    ox = int(offset[0]) % step
    oy = int(offset[1]) % step

    row = 0
    y = -oy
    while y < height:
        col = 0
        x = -ox
        while x < width:
            if (row + col) % 2:
                draw.rectangle([x, y, x + square - 1, y + square - 1], fill=dark)
            x += square
            col += 1
        y += square
        row += 1
    return img


def save_image(img: Image.Image, path: str, fmt: str, quality: int = 95) -> None:
    """按指定格式保存，JPEG 会自动把透明区域压到白底。"""
    if fmt == "JPEG":
        out = img
        if out.mode == "RGBA":
            bg = Image.new("RGB", out.size, (255, 255, 255))
            bg.paste(out, mask=out.split()[3])
            out = bg
        elif out.mode != "RGB":
            out = out.convert("RGB")
        out.save(path, "JPEG", quality=int(quality), subsampling=0)
    elif fmt == "WEBP":
        img.save(path, "WEBP", quality=int(quality))
    else:
        img.save(path, "PNG")