"""交互视图：可缩放平移的画布基类、裁剪视图、合成画布视图。"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import imaging
import model

HANDLE = 6          # 控制点命中半径（屏幕像素）
MIN_CROP = 4        # 最小裁剪尺寸（图像像素）
SNAP_SCREEN = 8     # 自动贴紧判定距离（屏幕像素）
LASSO_STEP = 3      # 套索采点间隔（屏幕像素），太小会导致顶点过密
LASSO_MAX = 3000    # 套索顶点上限


def rgb_to_hex(color) -> str:
    r, g, b = color[0], color[1], color[2]
    return f"#{r:02x}{g:02x}{b:02x}"


# ================================================================ 基类

class ZoomPanCanvas(ttk.Frame):
    """支持滚轮缩放、中键拖拽平移的画布容器。"""

    def __init__(self, master, bg: str = "#3a3a3a", **kw):
        super().__init__(master, **kw)
        self.bg_color = bg
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.content_size = (1, 1)
        self.zoom = 1.0
        self.view_x = 0.0
        self.view_y = 0.0
        self.fit_mode = True
        self._photo_cache: dict = {}
        self._pan_anchor = None

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._on_pan_end)

    # ---------------- 坐标换算 ----------------

    def viewport(self) -> tuple[int, int]:
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def world_to_screen(self, wx: float, wy: float) -> tuple[float, float]:
        return (wx - self.view_x) * self.zoom, (wy - self.view_y) * self.zoom

    def screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        return sx / self.zoom + self.view_x, sy / self.zoom + self.view_y

    # ---------------- 缩放 ----------------

    def fit(self) -> None:
        cw, ch = self.viewport()
        w, h = self.content_size
        zoom = min((cw - 24) / max(1, w), (ch - 24) / max(1, h))
        self.zoom = min(40.0, max(0.01, zoom))
        self.view_x = (w - cw / self.zoom) / 2
        self.view_y = (h - ch / self.zoom) / 2
        self.fit_mode = True
        self._photo_cache.clear()
        self.redraw()

    def zoom_at(self, sx: float, sy: float, factor: float) -> None:
        wx, wy = self.screen_to_world(sx, sy)
        self.zoom = min(max(self.zoom * factor, 0.01), 40.0)
        self.view_x = wx - sx / self.zoom
        self.view_y = wy - sy / self.zoom
        self.fit_mode = False
        self._photo_cache.clear()
        self.redraw()

    def zoom_step(self, factor: float) -> None:
        cw, ch = self.viewport()
        self.zoom_at(cw / 2, ch / 2, factor)

    def zoom_reset(self) -> None:
        cw, ch = self.viewport()
        wx, wy = self.screen_to_world(cw / 2, ch / 2)
        self.zoom = 1.0
        self.view_x = wx - cw / 2
        self.view_y = wy - ch / 2
        self.fit_mode = False
        self._photo_cache.clear()
        self.redraw()

    # ---------------- 事件 ----------------

    def _on_configure(self, _event) -> None:
        if self.fit_mode:
            self.fit()
        else:
            self.redraw()

    def _on_wheel(self, event) -> None:
        self.zoom_at(event.x, event.y, 1.15 if event.delta > 0 else 1 / 1.15)

    def _on_pan_start(self, event) -> None:
        self._pan_anchor = (event.x, event.y, self.view_x, self.view_y)

    def _on_pan_move(self, event) -> None:
        if not self._pan_anchor:
            return
        sx, sy, vx, vy = self._pan_anchor
        self.view_x = vx - (event.x - sx) / self.zoom
        self.view_y = vy - (event.y - sy) / self.zoom
        self.fit_mode = False
        self.redraw()

    def _on_pan_end(self, _event) -> None:
        self._pan_anchor = None

    # ---------------- 绘制 ----------------

    def redraw(self) -> None:
        self.canvas.delete("all")
        self.draw_background()
        self.draw_content()
        self.draw_overlay()

    def draw_background(self) -> None:
        pass

    def draw_content(self) -> None:
        pass

    def draw_overlay(self) -> None:
        pass

    def visible_world(self) -> tuple[float, float, float, float]:
        """当前视口对应的世界坐标范围。"""
        cw, ch = self.viewport()
        x0, y0 = self.screen_to_world(0, 0)
        x1, y1 = self.screen_to_world(cw, ch)
        return x0, y0, x1, y1

    def draw_image(self, img: Image.Image, dest: tuple[float, float, float, float],
                   crop_box=None) -> None:
        """把 img（可先按 crop_box 裁剪）铺到世界坐标矩形 dest=(x, y, w, h) 上。

        只渲染视口内的那一部分像素，因此单个位图的大小恒不超过视口尺寸。
        这样即使源图极大或缩放倍率很高，也不会把内存撑爆。
        """
        x, y, w, h = dest
        if w <= 0 or h <= 0:
            return
        vx0, vy0, vx1, vy1 = self.visible_world()
        ix0, iy0 = max(x, vx0), max(y, vy0)
        ix1, iy1 = min(x + w, vx1), min(y + h, vy1)
        if ix1 - ix0 <= 0 or iy1 - iy0 <= 0:
            return

        src = img.crop(crop_box) if crop_box else img
        sw, sh = src.size
        if sw < 1 or sh < 1:
            return
        fx, fy = sw / w, sh / h
        bx0 = max(0, int(math.floor((ix0 - x) * fx)))
        by0 = max(0, int(math.floor((iy0 - y) * fy)))
        bx1 = min(sw, int(math.ceil((ix1 - x) * fx)))
        by1 = min(sh, int(math.ceil((iy1 - y) * fy)))
        if bx1 <= bx0 or by1 <= by0:
            return

        tw = max(1, int(round((bx1 - bx0) / fx * self.zoom)))
        th = max(1, int(round((by1 - by0) / fy * self.zoom)))
        key = (id(img), crop_box, bx0, by0, bx1, by1, tw, th)
        photo = self._photo_cache.get(key)
        if photo is None:
            region = src.crop((bx0, by0, bx1, by1))
            scaled = region if region.size == (tw, th) else region.resize((tw, th), Image.LANCZOS)
            photo = ImageTk.PhotoImage(scaled)
            if len(self._photo_cache) > 120:
                self._photo_cache.clear()
            self._photo_cache[key] = photo

        sx, sy = self.world_to_screen(ix0, iy0)
        self.canvas.create_image(sx, sy, image=photo, anchor="nw")

    def _hint(self, text: str, color: str = "#dddddd") -> None:
        cw, ch = self.viewport()
        self.canvas.create_text(cw / 2, ch / 2, text=text, fill=color,
                                font=("Microsoft YaHei", 12), justify="center")


# ================================================================ 裁剪视图

class CropView(ZoomPanCanvas):
    """在源图上做矩形框选、不规则多边形框选或自由套索框选。"""

    def __init__(self, master, on_piece_created, push_undo=None, request_crop=None):
        super().__init__(master, bg="#2b2b2b")
        self.on_piece_created = on_piece_created
        self.push_undo = push_undo     # 由主窗口传入，用于裁剪前记录撤回快照
        self.request_crop = request_crop   # 由主窗口传入，保证裁剪时套用最新的边框设置

        self.image: Image.Image | None = None
        self.source_name = ""
        self.mode = "矩形"           # 矩形 / 多边形 / 套索
        self.border = imaging.BORDER_NONE
        self.border_width = 0

        self.rect: list[float] | None = None
        self.points: list[tuple[float, float]] = []
        self.poly_closed = False
        self._pending_crop = False     # 刚闭合，等一个鼠标事件再自动裁剪
        self._cursor = (0.0, 0.0)
        self._drag = None
        self._crop_count = 0

        c = self.canvas
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Motion>", self._on_motion)
        c.bind("<ButtonPress-3>", self._on_right_click)
        c.bind("<Enter>", lambda e: c.focus_set())
        c.bind("<Escape>", lambda e: self.clear_selection())
        c.bind("<BackSpace>", self._on_backspace)

    # ---------------- 状态 ----------------

    def set_image(self, image: Image.Image | None, name: str = "") -> None:
        self.image = image
        self.source_name = name
        self._crop_count = 0
        self.rect = None
        self.points = []
        self.poly_closed = False
        self._pending_crop = False
        self._drag = None
        # 先更新内容尺寸，再直接按新尺寸重算缩放，避免用上一个内容的缩放倍率重绘
        self.content_size = (image.width, image.height) if image else (1, 1)
        self.fit()

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.canvas.configure(cursor="crosshair" if mode in ("矩形", "套索") else "")
            self.clear_selection()

    def set_border(self, border: str, width: int) -> None:
        self.border = border
        self.border_width = int(width)

    def clear_selection(self) -> None:
        self.rect = None
        self.points = []
        self.poly_closed = False
        self._pending_crop = False
        self._drag = None
        self.redraw()

    def _clamp(self, wx: float, wy: float) -> tuple[float, float]:
        if self.image is None:
            return wx, wy
        return (min(max(wx, 0), self.image.width), min(max(wy, 0), self.image.height))

    # ---------------- 鼠标 ----------------

    def _on_press(self, event) -> None:
        if self.image is None:
            return
        self.canvas.focus_set()
        # 上一笔刚闭合：本次点击视为确认裁剪（给用户一次反悔机会，不点就不裁）
        if self._pending_crop:
            self._pending_crop = False
            self._request_crop()
            return
        wx, wy = self._clamp(*self.screen_to_world(event.x, event.y))
        if self.mode == "多边形":
            self._polygon_press(event.x, event.y, wx, wy)
        elif self.mode == "套索":
            self._lasso_press(wx, wy)
        else:
            self._rect_press(event.x, event.y, wx, wy)
        self.redraw()

    # ---- 矩形 ----

    def _rect_handles(self) -> dict:
        if not self.rect:
            return {}
        x0, y0, x1, y1 = self.rect
        return {"nw": (x0, y0), "ne": (x1, y0), "sw": (x0, y1), "se": (x1, y1)}

    def _hit_handle(self, sx: float, sy: float) -> str | None:
        for name, (hx, hy) in self._rect_handles().items():
            px, py = self.world_to_screen(hx, hy)
            if abs(px - sx) <= HANDLE and abs(py - sy) <= HANDLE:
                return name
        return None

    def _rect_press(self, sx: float, sy: float, wx: float, wy: float) -> None:
        if self.rect:
            handle = self._hit_handle(sx, sy)
            if handle:
                self._drag = ("handle", handle, tuple(self.rect))
                return
            x0, y0, x1, y1 = self.rect
            nx0, nx1 = min(x0, x1), max(x0, x1)
            ny0, ny1 = min(y0, y1), max(y0, y1)
            if nx0 <= wx <= nx1 and ny0 <= wy <= ny1:
                self._drag = ("move", (wx, wy), tuple(self.rect))
                return
        self.rect = [wx, wy, wx, wy]
        self._drag = ("new", (wx, wy), None)

    # ---- 多边形 ----

    def _polygon_press(self, sx: float, sy: float, wx: float, wy: float) -> None:
        if self.poly_closed:
            self.points = []
            self.poly_closed = False

        # 「点击起点闭合」必须排在「拖拽已有点」之前：起点同时就是第 0 个顶点，
        # 若先判顶点，点击起点永远会被当成拖动第 0 个顶点，导致无法闭合。
        if len(self.points) >= 3:
            fx, fy = self.points[0]
            hx, hy = self.world_to_screen(fx, fy)
            if (hx - sx) ** 2 + (hy - sy) ** 2 <= (HANDLE + 4) ** 2:
                self.poly_closed = True
                self._pending_crop = True
                return

        for i, (px, py) in enumerate(self.points):
            hx, hy = self.world_to_screen(px, py)
            if (hx - sx) ** 2 + (hy - sy) ** 2 <= (HANDLE + 2) ** 2:
                self._drag = ("vertex", i, None)
                return

        self.points.append((wx, wy))

    def _on_backspace(self, _event) -> None:
        if self.mode == "多边形" and self.points and not self.poly_closed:
            self.points.pop()
            self.redraw()

    def _on_right_click(self, _event) -> None:
        """多边形 / 套索模式下右键：未闭合则先闭合，已闭合则加入素材库。"""
        if self.mode not in ("多边形", "套索"):
            return
        if self._pending_crop:               # 已闭合，右键即为「确认入库」
            self._pending_crop = False
            self._request_crop()
        elif not self.poly_closed and len(self.points) >= 3:
            self.poly_closed = True          # 只闭合，入库留给下一次点击
            self._pending_crop = True
            self.redraw()
        else:
            self.clear_selection()

    # ---- 套索 ----

    def _lasso_press(self, wx: float, wy: float) -> None:
        """按下左键开始套索，从当前位置开始记点。"""
        self.points = [(wx, wy)]
        self.poly_closed = False
        self._cursor = (wx, wy)
        self._drag = ("lasso", (wx, wy), None)

    def _lasso_add(self, wx: float, wy: float) -> None:
        """套索拖拽中追加顶点，按屏幕距离抽稀，避免点太密。"""
        if not self.points:
            self.points = [(wx, wy)]
            return
        if len(self.points) >= LASSO_MAX:
            return
        lx, ly = self.points[-1]
        sx0, sy0 = self.world_to_screen(lx, ly)
        sx1, sy1 = self.world_to_screen(wx, wy)
        if (sx1 - sx0) ** 2 + (sy1 - sy0) ** 2 < LASSO_STEP ** 2:
            return
        self.points.append((wx, wy))

    def _lasso_release(self) -> None:
        """松开左键自动闭合；先闭合不入库，等下一次点击确认。"""
        pts = self.points
        if len(pts) >= 3:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if max(xs) - min(xs) >= MIN_CROP and max(ys) - min(ys) >= MIN_CROP:
                self.poly_closed = True
                self._pending_crop = True
                return
        self.points = []                     # 圈得太小，当误操作丢掉
        self.poly_closed = False

    def _request_crop(self) -> None:
        """请求裁剪：优先走主窗口入口，以便先同步边框设置。"""
        if self.request_crop is not None:
            self.request_crop()
        else:
            self.crop_now()

    # ---- 拖拽 ----

    def _on_drag(self, event) -> None:
        if self.image is None or not self._drag:
            return
        wx, wy = self._clamp(*self.screen_to_world(event.x, event.y))
        self._cursor = (wx, wy)
        kind = self._drag[0]

        if kind == "new":
            ax, ay = self._drag[1]
            self.rect = [ax, ay, wx, wy]
        elif kind == "move":
            (ax, ay), orig = self._drag[1], self._drag[2]
            dx, dy = wx - ax, wy - ay
            self.rect = [orig[0] + dx, orig[1] + dy, orig[2] + dx, orig[3] + dy]
        elif kind == "handle":
            name, orig = self._drag[1], list(self._drag[2])
            if "n" in name:
                orig[1] = wy
            if "s" in name:
                orig[3] = wy
            if "w" in name:
                orig[0] = wx
            if "e" in name:
                orig[2] = wx
            self.rect = orig
        elif kind == "vertex":
            idx = self._drag[1]
            self.points[idx] = (wx, wy)
        elif kind == "lasso":
            self._lasso_add(wx, wy)

        if self.rect:
            x0, y0 = self._clamp(self.rect[0], self.rect[1])
            x1, y1 = self._clamp(self.rect[2], self.rect[3])
            self.rect = [x0, y0, x1, y1]
        self.redraw()

    def _on_release(self, _event) -> None:
        if self._drag and self._drag[0] == "lasso":
            self._lasso_release()            # 松开即闭合，暂不入库
        if self.rect:
            x0, y0, x1, y1 = self.rect
            if abs(x1 - x0) < MIN_CROP or abs(y1 - y0) < MIN_CROP:
                self.rect = None
        self._drag = None
        self.redraw()

    def _on_motion(self, event) -> None:
        self._cursor = self.screen_to_world(event.x, event.y)
        if self.mode in ("多边形", "套索") and self.points and not self.poly_closed:
            self.redraw()
        elif self.mode == "矩形":
            self._update_cursor(event.x, event.y)

    def _update_cursor(self, sx: float, sy: float) -> None:
        if not self.rect:
            self.canvas.configure(cursor="crosshair")
            return
        handle = self._hit_handle(sx, sy)
        if handle in ("nw", "se"):
            self.canvas.configure(cursor="size_nw_se")
        elif handle in ("ne", "sw"):
            self.canvas.configure(cursor="size_ne_sw")
        else:
            self.canvas.configure(cursor="crosshair")

    # ---------------- 裁剪 ----------------

    def crop_now(self) -> model.CropPiece | None:
        """把当前选区裁剪成素材。"""
        if self.image is None:
            return None
        outline = None
        try:
            if self.mode in ("多边形", "套索"):
                if len(self.points) < 3:
                    raise ValueError("请先圈出至少 3 个顶点")
                base, outline = imaging.crop_polygon(self.image, self.points)
            else:
                if not self.rect:
                    raise ValueError("请先在图片上框选要裁剪的区域")
                x0, y0, x1, y1 = imaging.normalize_box(self.rect)
                if x1 - x0 < MIN_CROP or y1 - y0 < MIN_CROP:
                    raise ValueError("选区太小，请重新框选")
                base = imaging.crop_rect(self.image, (x0, y0, x1, y1))
        except ValueError as exc:
            messagebox.showwarning("无法裁剪", str(exc), parent=self.winfo_toplevel())
            return None

        self._crop_count += 1
        stem = self.source_name.rsplit(".", 1)[0] or "素材"
        piece = model.CropPiece(
            base=base,
            name=f"{stem}_{self._crop_count:02d}",
            source=self.source_name,
            border=self.border,
            border_width=self.border_width,
            outline=tuple(outline) if outline else None,
        )
        self.clear_selection()
        if self.push_undo:          # 入素材库前记录快照，供 Ctrl+Z 撤回
            self.push_undo()
        self.on_piece_created(piece)
        return piece

    # ---------------- 绘制 ----------------

    def draw_background(self) -> None:
        cw, ch = self.viewport()
        self.canvas.create_rectangle(0, 0, cw, ch, fill=self.bg_color, outline="")
        if self.image is None:
            self._hint("左侧导入图片后，在这里框选要裁剪的区域\n\n"
                       "矩形：按住左键拖拽\n"
                       "多边形：依次点击添加顶点，点击起点或右键闭合\n"
                       "套索：按住左键随意描边，松开即闭合\n"
                       "以上三种都是「先闭合，再点一下画布或点『加入素材库』」完成\n"
                       "滚轮缩放 · 中键拖拽平移 · 后退键退掉上一个顶点")
            return
        x0, y0 = self.world_to_screen(0, 0)
        x1, y1 = self.world_to_screen(self.image.width, self.image.height)
        self.canvas.create_rectangle(x0, y0, x1, y1, fill="#000000", outline="")

    def draw_content(self) -> None:
        if self.image is None:
            return
        self.draw_image(self.image, (0, 0, self.image.width, self.image.height))

    def draw_overlay(self) -> None:
        if self.image is None:
            return
        if self.mode == "矩形":
            self._draw_rect_overlay()
        else:
            self._draw_polygon_overlay()

    def _draw_rect_overlay(self) -> None:
        if not self.rect:
            return
        x0, y0, x1, y1 = imaging.normalize_box(self.rect)
        sx0, sy0 = self.world_to_screen(x0, y0)
        sx1, sy1 = self.world_to_screen(x1, y1)

        # 选区外遮罩（四条带）
        img_w, img_h = self.image.size
        ix0, iy0 = self.world_to_screen(0, 0)
        ix1, iy1 = self.world_to_screen(img_w, img_h)
        for box in ((ix0, iy0, ix1, sy0), (ix0, sy1, ix1, iy1),
                    (ix0, sy0, sx0, sy1), (sx1, sy0, ix1, sy1)):
            self.canvas.create_rectangle(*box, fill="#000000", outline="", stipple="gray50")

        self.canvas.create_rectangle(sx0, sy0, sx1, sy1, outline="#2d8cf0", width=2)
        for hx, hy in self._rect_handles().values():
            px, py = self.world_to_screen(hx, hy)
            self.canvas.create_rectangle(px - 4, py - 4, px + 4, py + 4,
                                         fill="#2d8cf0", outline="#ffffff")
        label = f"{x1 - x0} × {y1 - y0}"
        self.canvas.create_text(sx0 + 4, sy0 - 12, text=label, anchor="w",
                                fill="#ffffff", font=("Microsoft YaHei", 9))

    def _draw_polygon_overlay(self) -> None:
        if not self.points:
            return
        screen = [self.world_to_screen(px, py) for px, py in self.points]
        flat = [v for pt in screen for v in pt]

        if len(screen) >= 2:
            self.canvas.create_line(*flat, fill="#2d8cf0", width=2)
        if self.poly_closed and len(screen) >= 3:
            self.canvas.create_polygon(*flat, fill="#2d8cf0", outline="#2d8cf0",
                                       stipple="gray25", width=2)
        elif len(screen) >= 2 and self.mode == "多边形":
            cx, cy = self.world_to_screen(*self._cursor)
            self.canvas.create_line(screen[-1][0], screen[-1][1], cx, cy,
                                    fill="#2d8cf0", width=1, dash=(4, 3))

        if self.mode == "套索":
            # 套索顶点很密，只标出起点，避免满屏小圆点
            px, py = screen[0]
            self.canvas.create_oval(px - 4, py - 4, px + 4, py + 4,
                                    fill="#ffcc00", outline="#ffffff")
        else:
            for i, (px, py) in enumerate(screen):
                color = "#ffcc00" if i == 0 else "#2d8cf0"
                self.canvas.create_oval(px - 4, py - 4, px + 4, py + 4,
                                        fill=color, outline="#ffffff")
        if self.poly_closed:
            self.canvas.create_text(screen[0][0] + 8, screen[0][1] - 14,
                                    text="已闭合 · 再点一下画布或点「加入素材库」完成裁剪",
                                    anchor="w", fill="#ffffff",
                                    font=("Microsoft YaHei", 9))
        elif len(self.points) >= 3 and self.mode == "多边形":
            self.canvas.create_text(screen[0][0] + 8, screen[0][1] - 14,
                                    text="点击起点 / 右键 闭合多边形",
                                    anchor="w", fill="#ffffff",
                                    font=("Microsoft YaHei", 9))


# ================================================================ 合成画布视图

class ComposeView(ZoomPanCanvas):
    """成品画布：自由拖拽摆放、自动贴紧，并预览顺序拼接 / 网格排版。"""

    def __init__(self, master, doc: model.Composition, on_select=None, on_change=None,
                 push_undo=None):
        super().__init__(master, bg="#333333")
        self.doc = doc
        self.on_select = on_select
        self.on_change = on_change
        self.push_undo = push_undo     # 由主窗口传入，用于改动前记录撤回快照

        self.selected: model.PlacedItem | None = None
        self._drag = None
        self._drag_pushed = False
        self._guides: tuple[list, list] = ([], [])
        self._rects: dict[int, tuple[float, float, float, float]] = {}
        self._checker_cache: tuple | None = None
        self._checker_photo = None
        self.checker_colors = ((255, 255, 255), (205, 205, 205))

        c = self.canvas
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Delete>", lambda e: self.delete_selected())
        c.bind("<Enter>", lambda e: c.focus_set())

    # ---------------- 刷新 ----------------

    def refresh(self, refit: bool = False) -> None:
        size, _ = self.doc.preview()
        self.content_size = (max(1, size[0]), max(1, size[1]))
        if refit or self.fit_mode:
            self.fit()
        else:
            self.redraw()

    def set_checker_colors(self, light: tuple, dark: tuple) -> None:
        """透明底棋盘格跟随主题换色。"""
        self.checker_colors = (light, dark)
        self._checker_cache = None

    def select(self, item: model.PlacedItem | None, raise_to_top: bool = False) -> None:
        if raise_to_top and item is not None:
            self.doc.bring_to_top(item)
        self.selected = item
        if self.on_select:
            self.on_select(item)
        self.redraw()

    def delete_selected(self) -> None:
        if self.selected is None:
            return
        if self.push_undo:
            self.push_undo()
        self.doc.remove(self.selected)
        self.selected = None
        if self.on_select:
            self.on_select(None)
        self.refresh()
        if self.on_change:
            self.on_change()

    # ---------------- 命中与拖拽 ----------------

    def _item_rect(self, item: model.PlacedItem):
        rect = self._rects.get(item.iid)
        if rect is not None:
            return rect
        if self.doc.mode == imaging.MODE_FREE:
            w, h = item.size
            return (item.x, item.y, w, h)
        return None

    def _hit_item(self, wx: float, wy: float):
        for item in reversed(self.doc.items):
            rect = self._item_rect(item)
            if rect is None:
                continue
            x, y, w, h = rect
            if x <= wx <= x + w and y <= wy <= y + h:
                return item
        return None

    def _on_press(self, event) -> None:
        self.canvas.focus_set()
        wx, wy = self.screen_to_world(event.x, event.y)
        item = self._hit_item(wx, wy)
        self._guides = ([], [])
        if item is None:
            self.select(None)
            return
        # 置顶会改变层序，本身就是一次内容改动
        will_raise = bool(self.doc.items) and self.doc.items[-1] is not item
        if will_raise and self.push_undo:
            self.push_undo()
        self.select(item, raise_to_top=True)
        self._drag = None
        self._drag_pushed = will_raise
        if self.doc.mode == imaging.MODE_FREE:
            self._drag = (wx, wy, item.x, item.y)

    def _on_drag(self, event) -> None:
        if not self._drag or self.selected is None:
            return
        if not self._drag_pushed:      # 真正拖动时才记快照，避免单纯点选产生撤回步骤
            if self.push_undo:
                self.push_undo()
            self._drag_pushed = True
        wx, wy = self.screen_to_world(event.x, event.y)
        ax, ay, ox, oy = self._drag
        nx, ny = ox + (wx - ax), oy + (wy - ay)
        w, h = self.selected.size
        if self.doc.snap:
            nx, ny, self._guides = self._snap(self.selected, nx, ny, w, h)
        else:
            self._guides = ([], [])
        self.selected.x, self.selected.y = nx, ny
        self.redraw()

    def _on_release(self, _event) -> None:
        if self._drag is not None:
            self._drag = None
            self._drag_pushed = False
            self._guides = ([], [])
            if self.selected is not None and self.on_change:
                self.on_change()
            self.redraw()

    # ---------------- 自动贴紧 ----------------

    def _snap(self, item: model.PlacedItem, x: float, y: float, w: float, h: float):
        """把元素吸附到相邻元素 / 画布边界的边缘或中线上，避免重叠或留缝。"""
        threshold = SNAP_SCREEN / self.zoom
        cw, ch = self.content_size

        x_targets = [0.0, cw / 2, float(cw)]
        y_targets = [0.0, ch / 2, float(ch)]
        for other in self.doc.items:
            if other is item:
                continue
            rect = self._item_rect(other)
            if rect is None:
                continue
            ox, oy, ow, oh = rect
            x_targets += [ox, ox + ow / 2, ox + ow]
            y_targets += [oy, oy + oh / 2, oy + oh]

        def best(targets, edges):
            chosen_delta, chosen_line = None, None
            for target in targets:
                for edge in edges:
                    delta = target - edge
                    if abs(delta) <= threshold and (chosen_delta is None or abs(delta) < abs(chosen_delta)):
                        chosen_delta, chosen_line = delta, target
            return chosen_delta, chosen_line

        dx, gx = best(x_targets, (x, x + w / 2, x + w))
        dy, gy = best(y_targets, (y, y + h / 2, y + h))

        guides_x = [gx] if gx is not None else []
        guides_y = [gy] if gy is not None else []
        return x + (dx or 0.0), y + (dy or 0.0), (guides_x, guides_y)

    # ---------------- 绘制 ----------------

    def draw_background(self) -> None:
        cw, ch = self.viewport()
        self.canvas.create_rectangle(0, 0, cw, ch, fill=self.bg_color, outline="")

        w, h = self.content_size
        x0, y0 = self.world_to_screen(0, 0)
        x1, y1 = self.world_to_screen(w, h)

        if self.doc.background is None:
            ix0, iy0 = max(0, int(x0)), max(0, int(y0))
            ix1, iy1 = min(cw, int(x1)), min(ch, int(y1))
            if ix1 > ix0 and iy1 > iy0:
                photo = self._checker(ix1 - ix0, iy1 - iy0, int(ix0 - x0), int(iy0 - y0))
                self.canvas.create_image(ix0, iy0, image=photo, anchor="nw")
        else:
            self.canvas.create_rectangle(x0, y0, x1, y1,
                                         fill=rgb_to_hex(self.doc.background), outline="")
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#909090", dash=(4, 3))

    def _checker(self, w: int, h: int, ox: int, oy: int):
        key = (w, h, ox % 24, oy % 24)
        if self._checker_cache != key or self._checker_photo is None:
            light, dark = self.checker_colors
            self._checker_photo = ImageTk.PhotoImage(
                imaging.make_checker(w, h, 12, (ox, oy), light, dark)
            )
            self._checker_cache = key
        return self._checker_photo

    def draw_content(self) -> None:
        _size, placements = self.doc.preview()
        self._rects = {}
        items = self.doc.items
        for i, (img, x, y, w, h, crop) in enumerate(placements):
            self.draw_image(img, (x, y, w, h), crop)
            if i < len(items):
                self._rects[items[i].iid] = (x, y, w, h)

        if not items:
            if self.doc.mode == imaging.MODE_FREE:
                self._hint("画布还是空的\n\n从右侧「素材库」点「添加到画布」")
            else:
                self._hint(f"{self.doc.mode}模式：点「全部加入」把素材排进来")

    def draw_overlay(self) -> None:
        for gx in self._guides[0]:
            sx, _ = self.world_to_screen(gx, 0)
            cw, ch = self.viewport()
            self.canvas.create_line(sx, 0, sx, ch, fill="#ff4081", width=1, dash=(5, 3))
        for gy in self._guides[1]:
            _, sy = self.world_to_screen(0, gy)
            cw, ch = self.viewport()
            self.canvas.create_line(0, sy, cw, sy, fill="#ff4081", width=1, dash=(5, 3))

        if self.selected is None:
            return
        rect = self._item_rect(self.selected)
        if rect is None:
            return
        x, y, w, h = rect
        sx0, sy0 = self.world_to_screen(x, y)
        sx1, sy1 = self.world_to_screen(x + w, y + h)
        self.canvas.create_rectangle(sx0, sy0, sx1, sy1, outline="#2d8cf0", width=2)
        label = f"{self.selected.piece.name}  {int(w)}×{int(h)}"
        self.canvas.create_text(sx0, sy0 - 12, text=label, anchor="w",
                                fill="#ffffff", font=("Microsoft YaHei", 9))