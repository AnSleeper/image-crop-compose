"""数据模型：源图、裁剪素材、画布元素与合成文档。"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from PIL import Image

import imaging

_sid = count(1)
_pid = count(1)
_iid = count(1)

MAX_ITEM_PIXELS = 64_000_000   # 单个画布元素的像素上限，防止极端缩放撑爆内存


# ---------------------------------------------------------------- 源图

@dataclass
class SourceImage:
    """一张导入的原图，用于裁剪。"""

    image: Image.Image
    name: str
    path: str = ""
    sid: int = field(default_factory=lambda: next(_sid))

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def __str__(self) -> str:
        return f"{self.name}  {self.image.width}×{self.image.height}"


# ---------------------------------------------------------------- 裁剪素材

@dataclass
class CropPiece:
    """一次裁剪得到的素材（已保留透明通道），边框在渲染时实时套用。"""

    base: Image.Image
    name: str = "素材"
    source: str = ""
    border: str = imaging.BORDER_NONE
    border_width: int = 0
    outline: tuple | None = None   # 多边形裁剪的轮廓顶点，None 表示矩形
    pid: int = field(default_factory=lambda: next(_pid))
    _key: tuple | None = field(default=None, repr=False, compare=False)
    _img: Image.Image | None = field(default=None, repr=False, compare=False)

    def render(self) -> Image.Image:
        """返回套用边框后的素材图（带缓存）。"""
        key = (self.border, int(self.border_width))
        if self._img is None or self._key != key:
            self._img = imaging.apply_border(self.base, self.border,
                                             self.border_width, self.outline)
            self._key = key
        return self._img

    @property
    def size(self) -> tuple[int, int]:
        return self.render().size

    def __str__(self) -> str:
        w, h = self.size
        return f"{self.name}  {w}×{h}"


# ---------------------------------------------------------------- 画布元素

@dataclass
class PlacedItem:
    """素材在画布上的一次摆放（可缩放 / 旋转 / 翻转）。"""

    piece: CropPiece
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rot: int = 0
    flip_h: bool = False
    flip_v: bool = False
    iid: int = field(default_factory=lambda: next(_iid))
    _key: tuple | None = field(default=None, repr=False, compare=False)
    _img: Image.Image | None = field(default=None, repr=False, compare=False)

    def transform_key(self) -> tuple:
        return (self.piece.pid, self.piece.border, int(self.piece.border_width),
                round(float(self.scale), 6), int(self.rot) % 360, self.flip_h, self.flip_v)

    def render(self) -> Image.Image:
        """返回变换后的最终图像（带缓存）。"""
        key = self.transform_key()
        if self._img is None or self._key != key:
            img = self.piece.render()
            if self.flip_h:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            if self.flip_v:
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            rot = int(self.rot) % 360
            if rot:
                img = img.rotate(-rot, expand=True)  # 顺时针旋转
            scale = float(self.scale)
            if abs(scale - 1.0) > 1e-6:
                w = max(1, int(round(img.width * scale)))
                h = max(1, int(round(img.height * scale)))
                if w * h > MAX_ITEM_PIXELS:
                    k = (MAX_ITEM_PIXELS / (w * h)) ** 0.5
                    w, h = max(1, int(w * k)), max(1, int(h * k))
                img = img.resize((w, h), Image.LANCZOS)
            self._img = img
            self._key = key
        return self._img

    @property
    def size(self) -> tuple[int, int]:
        return self.render().size

    @property
    def rect(self) -> tuple[float, float, float, float]:
        w, h = self.size
        return (self.x, self.y, self.x + w, self.y + h)

    def __str__(self) -> str:
        return f"{self.piece.name} @ ({self.x:.0f}, {self.y:.0f})"


# ---------------------------------------------------------------- 合成文档

class Composition:
    """整张成品图的排版状态。"""

    def __init__(self) -> None:
        self.items: list[PlacedItem] = []       # 自下而上，最后一个在最上层
        self.canvas_w = 1200
        self.canvas_h = 800
        self.background = imaging.TRANSPARENT   # None 表示透明
        self.mode = imaging.MODE_FREE
        self.snap = True

        # 顺序拼接参数
        self.stitch_direction = imaging.STITCH_HORIZONTAL
        self.stitch_spacing = 0
        self.stitch_align = imaging.ALIGN_START

        # 网格参数
        self.grid_rows = 2
        self.grid_cols = 2
        self.grid_gap = 8
        self.grid_cell: tuple[int, int] | None = None
        self.grid_fit = imaging.FIT_CONTAIN

    # ---------------- 元素管理 ----------------

    def add(self, item: PlacedItem) -> None:
        self.items.append(item)

    def remove(self, item: PlacedItem) -> None:
        if item in self.items:
            self.items.remove(item)

    def bring_to_top(self, item: PlacedItem) -> None:
        """把元素移到最上层（默认最后操作的图片在上层）。"""
        if item in self.items and self.items[-1] is not item:
            self.items.remove(item)
            self.items.append(item)

    def move_layer(self, item: PlacedItem, delta: int) -> None:
        if item not in self.items:
            return
        i = self.items.index(item)
        j = max(0, min(len(self.items) - 1, i + delta))
        if i != j:
            self.items.pop(i)
            self.items.insert(j, item)

    # ---------------- 排版 ----------------

    def _placements(self) -> list[tuple]:
        """自由模式：按元素自身坐标生成排版项。"""
        return [(it.render(), it.x, it.y, it.size[0], it.size[1], None) for it in self.items]

    def pages(self) -> list[tuple]:
        """返回 [(整体尺寸, [(img, x, y, w, h, crop_box), ...]), ...]。

        自由 / 顺序拼接只有一页；网格模式素材超出格子数时自动分页。
        """
        if self.mode == imaging.MODE_STITCH:
            images = [it.render() for it in self.items]
            if not images:
                return [((self.canvas_w, self.canvas_h), [])]
            positions, size = imaging.stitch_positions(
                [img.size for img in images],
                self.stitch_direction,
                self.stitch_spacing,
                self.stitch_align,
            )
            placements = [
                (img, x, y, img.width, img.height, None)
                for img, (x, y) in zip(images, positions)
            ]
            return [(size, placements)]

        if self.mode == imaging.MODE_GRID:
            images = [it.render() for it in self.items]
            if not images:
                return [((self.canvas_w, self.canvas_h), [])]
            per_page = max(1, self.grid_rows * self.grid_cols)
            pages = []
            for start in range(0, len(images), per_page):
                chunk = images[start:start + per_page]
                size, layout = imaging.grid_layout(
                    [img.size for img in chunk],
                    self.grid_rows,
                    self.grid_cols,
                    self.grid_gap,
                    self.grid_cell,
                    self.grid_fit,
                )
                placements = [
                    (chunk[i], x, y, w, h, crop)
                    for i, (x, y, w, h, crop) in enumerate(layout)
                ]
                pages.append((size, placements))
            return pages

        # 自由模式
        return [((self.canvas_w, self.canvas_h), self._placements())]

    def preview(self) -> tuple:
        """第一页 (尺寸, 排版)，供画布预览使用。"""
        return self.pages()[0]

    def content_bounds(self):
        """自由模式下所有元素的包围盒。"""
        return imaging.content_bounds(self._placements())