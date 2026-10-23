"""主窗口：源图管理、裁剪、素材库、画布合成与导出。"""
from __future__ import annotations

import json
import os
import queue
import sys
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

import imaging
import model
import views

OPEN_TYPES = [
    ("图片文件", "*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff"),
    ("所有文件", "*.*"),
]
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
THUMB = 56


def _app_dir() -> str:
    """程序所在目录：打包后取 exe 目录，源码运行时取脚本目录。

    设置文件按此目录存放，因此整个文件夹挪到别处，设置依然跟着走。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_FILE = os.path.join(_app_dir(), "settings.json")

# 两套配色：浅色沿用系统原生主题，深色使用 clam 主题 + 自定义配色
PALETTES = {
    "light": {
        "bg": "#f0f0f0", "panel": "#f5f5f5", "field": "#ffffff", "fg": "#1a1a1a",
        "dim": "#666666", "border": "#b5b5b5", "accent": "#2d8cf0",
        "active": "#e5e5e5", "disabled": "#9a9a9a",
        "canvas": "#2b2b2b", "canvas_alt": "#333333",
        "checker": ((255, 255, 255), (205, 205, 205)),
        "check_edge": "#9a9a9a",
    },
    "dark": {
        "bg": "#252526", "panel": "#2d2d30", "field": "#1b1b1c", "fg": "#e6e6e6",
        "dim": "#9d9d9d", "border": "#3f3f46", "accent": "#2d8cf0",
        "active": "#3a3a3d", "disabled": "#6b6b6b",
        "canvas": "#191919", "canvas_alt": "#202020",
        "checker": ((64, 64, 66), (48, 48, 50)),
        "check_edge": "#75757c",
    },
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("图片自由裁剪")
        self.geometry("1560x920")
        self.minsize(1180, 720)

        self.sources: list[model.SourceImage] = []
        self.pieces: list[model.CropPiece] = []
        self.doc = model.Composition()

        self._tray_thumbs: dict[int, ImageTk.PhotoImage] = {}
        self._panel_lock = False
        self.dnd_hint = ""
        self.saved = self._load_settings()   # 上次退出时保存的设置
        self.last_export_dir = self.saved.get(
            "export_dir", os.path.join(os.path.expanduser("~"), "Pictures"))
        self.history: list[dict] = []      # 撤回栈：存放每次改动前的状态快照
        self._history_limit = 50
        self._restoring = False
        self._check_icons = None
        self._check_seq = 0
        self._poll_id = None

        self._init_style()
        self._init_vars()
        self._build_ui()
        self.crop_view.set_mode(self.var_crop_mode.get())   # 用上次的裁剪方式
        self._apply_theme()
        self.refresh_sources()
        self.refresh_tray()
        self.refresh_canvas(refit=True)
        self.update_status()
        self._init_dnd()
        # 全局绑定：无论焦点在哪个控件上，Ctrl+Z 都能撤回
        self.bind_all("<Control-z>", self.undo)
        self.bind_all("<Control-Z>", self.undo)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ============================================================ 设置持久化

    def _load_settings(self) -> dict:
        """读取程序目录下的 settings.json，读不到就用空设置。"""
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        """把当前设置写回程序目录，下次启动直接沿用。"""
        data = {
            "dark": bool(self.var_dark.get()),
            "export_dir": self.last_export_dir,
            "crop_mode": self.var_crop_mode.get(),
            "crop_border": self.var_crop_border.get(),
            "crop_border_w": int(self.var_crop_border_w.get() or 0),
            "mode": self.var_mode.get(),
            "bg_kind": self.var_bg_kind.get(),
            "bg_color": list(self.background_color),
            "snap": bool(self.var_snap.get()),
            "stitch_dir": self.var_stitch_dir.get(),
            "stitch_gap": int(self.var_stitch_gap.get() or 0),
            "stitch_align": self.var_stitch_align.get(),
            "grid_rows": int(self.var_grid_rows.get() or 1),
            "grid_cols": int(self.var_grid_cols.get() or 1),
            "grid_gap": int(self.var_grid_gap.get() or 0),
            "grid_cell": self.var_grid_cell.get(),
            "grid_cw": int(self.var_grid_cw.get() or 1),
            "grid_ch": int(self.var_grid_ch.get() or 1),
            "grid_fit": self.var_grid_fit.get(),
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass                        # 目录不可写时静默跳过，不影响使用

    def on_close(self) -> None:
        self._save_settings()
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)   # 否掉拖拽轮询，避免关窗后回调报错
            except tk.TclError:
                pass
            self._poll_id = None
        self.destroy()

    # ============================================================ 撤回

    def _snapshot(self) -> dict:
        """记录当前全部可改动状态。图像对象按引用共享，不做像素拷贝，开销很小。"""
        doc = self.doc
        return {
            "sources": list(self.sources),
            "pieces": list(self.pieces),
            "borders": [(p, p.border, int(p.border_width)) for p in self.pieces],
            "items": [(it, it.x, it.y, it.scale, it.rot, it.flip_h, it.flip_v)
                      for it in doc.items],
            "doc": (doc.canvas_w, doc.canvas_h, doc.background, doc.mode, doc.snap,
                    doc.stitch_direction, doc.stitch_spacing, doc.stitch_align,
                    doc.grid_rows, doc.grid_cols, doc.grid_gap, doc.grid_cell, doc.grid_fit),
        }

    def push_undo(self, _event=None) -> None:
        """在任何改动发生之前调用，压入一份「改动前」的快照。"""
        self.history.append(self._snapshot())
        if len(self.history) > self._history_limit:
            del self.history[0]

    def undo(self, _event=None) -> None:
        """Ctrl+Z：回退到上一步改动之前的状态，可连续回退。"""
        if not self.history:
            return
        self._restore(self.history.pop())
        self.update_status()

    def _restore(self, snap: dict) -> None:
        doc = self.doc
        self.sources = list(snap["sources"])
        self.pieces = list(snap["pieces"])
        for piece, border, width in snap["borders"]:
            piece.border, piece.border_width = border, width

        (doc.canvas_w, doc.canvas_h, doc.background, doc.mode, doc.snap) = snap["doc"][:5]
        (doc.stitch_direction, doc.stitch_spacing, doc.stitch_align, doc.grid_rows,
         doc.grid_cols, doc.grid_gap, doc.grid_cell, doc.grid_fit) = snap["doc"][5:]

        doc.items = [entry[0] for entry in snap["items"]]
        for item, x, y, scale, rot, flip_h, flip_v in snap["items"]:
            item.x, item.y, item.scale = x, y, scale
            item.rot, item.flip_h, item.flip_v = rot, flip_h, flip_v

        # 回填控件值时要加锁，避免这些 set 反向触发写入逻辑
        self._panel_lock = True
        try:
            self.var_mode.set(doc.mode)
            self.var_canvas_w.set(doc.canvas_w)
            self.var_canvas_h.set(doc.canvas_h)
            self.var_bg_kind.set("透明" if doc.background is None else "颜色")
            if doc.background is not None:
                self.background_color = tuple(doc.background[:3])
                self.bg_swatch.configure(bg=views.rgb_to_hex(doc.background))
            self.var_snap.set(bool(doc.snap))
            self.var_stitch_dir.set(doc.stitch_direction)
            self.var_stitch_gap.set(doc.stitch_spacing)
            self.var_stitch_align.set(doc.stitch_align)
            self.var_grid_rows.set(doc.grid_rows)
            self.var_grid_cols.set(doc.grid_cols)
            self.var_grid_gap.set(doc.grid_gap)
            self.var_grid_cell.set("自定义" if doc.grid_cell else "自动")
            if doc.grid_cell:
                self.var_grid_cw.set(doc.grid_cell[0])
                self.var_grid_ch.set(doc.grid_cell[1])
            self.var_grid_fit.set(doc.grid_fit)
        finally:
            self._panel_lock = False

        self.compose_view.selected = None
        self.on_item_selected(None)
        self._update_mode_bar()
        self.refresh_sources()

        # 让左侧列表的选中项与裁剪区显示的图保持一致
        self._restoring = True
        try:
            current = self.crop_view.image
            index = next((i for i, s in enumerate(self.sources) if s.image is current), None)
            self.source_list.selection_clear(0, "end")
            if index is None:
                self.crop_view.set_image(None)
            else:
                self.source_list.selection_set(index)
        finally:
            self._restoring = False

        self.refresh_tray()
        self.refresh_canvas()

    # ============================================================ 初始化

    def _init_style(self) -> None:
        style = ttk.Style(self)
        self._native_theme = "clam"
        for theme in ("vista", "xpnative", "winnative", "clam"):
            if theme in style.theme_names():
                self._native_theme = theme
                break
        style.theme_use(self._native_theme)
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Bar.TFrame", padding=6)

    def _apply_theme(self) -> None:
        """按当前模式套用配色（深色需要切到 clam 主题才能真正上色）。"""
        dark = bool(self.var_dark.get())
        style = ttk.Style(self)
        style.theme_use("clam" if dark else self._native_theme)
        p = PALETTES["dark" if dark else "light"]
        self.palette = p

        style.configure(".", background=p["bg"], foreground=p["fg"],
                        fieldbackground=p["field"], bordercolor=p["border"],
                        lightcolor=p["panel"], darkcolor=p["bg"], troughcolor=p["field"],
                        focuscolor=p["accent"], selectbackground=p["accent"],
                        selectforeground="#ffffff")
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["fg"])
        style.configure("Hint.TLabel", background=p["bg"], foreground=p["dim"])
        style.configure("Bar.TFrame", background=p["panel"], padding=6)
        style.configure("TButton", background=p["panel"], foreground=p["fg"],
                        bordercolor=p["border"], padding=(8, 3))
        style.map("TButton",
                  background=[("pressed", p["accent"]), ("active", p["active"]),
                              ("disabled", p["bg"])],
                  foreground=[("disabled", p["disabled"])])
        style.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        self._install_check_indicator(style, p)
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                        insertcolor=p["fg"], bordercolor=p["border"])
        style.configure("TSpinbox", fieldbackground=p["field"], foreground=p["fg"],
                        background=p["panel"], arrowcolor=p["fg"], bordercolor=p["border"],
                        insertcolor=p["fg"])
        style.map("TSpinbox", fieldbackground=[("readonly", p["field"])],
                  arrowcolor=[("disabled", p["disabled"])])
        style.configure("TCombobox", fieldbackground=p["field"], background=p["panel"],
                        foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["border"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field"])],
                  foreground=[("readonly", p["fg"])])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["panel"], foreground=p["dim"],
                        bordercolor=p["border"], padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", p["bg"])],
                  foreground=[("selected", p["accent"])])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                        foreground=p["fg"], bordercolor=p["border"], rowheight=THUMB + 12)
        style.map("Treeview", background=[("selected", p["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=p["panel"], foreground=p["fg"])
        style.configure("TPanedwindow", background=p["bg"])
        style.configure("Sash", sashthickness=6, gripcount=0)
        style.configure("Horizontal.TScale", background=p["bg"], troughcolor=p["field"])
        style.configure("Separator", background=p["border"])

        for option, value in (("background", p["field"]), ("foreground", p["fg"]),
                              ("selectBackground", p["accent"]),
                              ("selectForeground", "#ffffff")):
            self.option_add(f"*TCombobox*Listbox.{option}", value)

        self.source_list.configure(background=p["field"], foreground=p["fg"],
                                   selectbackground=p["accent"], selectforeground="#ffffff",
                                   highlightbackground=p["border"])
        self.compose_view.set_checker_colors(*p["checker"])
        for view, color in ((self.crop_view, p["canvas"]), (self.compose_view, p["canvas_alt"])):
            view.bg_color = color
            view.canvas.configure(bg=color)
            view.redraw()
        self.refresh_tray()   # 缩略图底色跟随主题

    def toggle_theme(self) -> None:
        self._apply_theme()
        self.update_status()
        self._save_settings()          # 深色模式等立刻落盘，避免异常退出丢失

    # ---------------- 勾选框指示器 ----------------

    def _check_icon(self, checked: bool, p: dict, size: int = 15) -> Image.Image:
        """画一个方框，选中时在里面画对勾。

        clam 主题自带的指示器画的是一把 ✗，所以这里用图片元素替换掉。
        """
        s = size * 4
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((2, 2, s - 3, s - 3), radius=10,
                               fill=p["field"], outline=p.get("check_edge", p["border"]),
                               width=5)
        if checked:
            draw.line([(s * 0.22, s * 0.52), (s * 0.42, s * 0.73), (s * 0.80, s * 0.27)],
                      fill=p["accent"], width=7, joint="curve")
        return img.resize((size, size), Image.LANCZOS)

    def _install_check_indicator(self, style: ttk.Style, p: dict) -> None:
        """用自绘的 √ 指示器替换主题默认的勾选标记。

        clam 主题画的是一把 ✗，所以统一换成自绘的 √（浅色深色各一套配色）。
        ttk 的 image 元素归属于「当前主题」、切换主题时会被清掉，而且 Tk 不提供删除元素的
        接口（删不掉的旧名字还会残留在 element_names 里，不能用来判断元素是否真的还在），
        因此这里每次都用一个全新的元素名，确保布局引用的元素一定存在。
        """
        self._check_seq += 1
        name = f"Check{self._check_seq}.TCheckbutton.indicator"
        on = ImageTk.PhotoImage(self._check_icon(True, p))
        off = ImageTk.PhotoImage(self._check_icon(False, p))
        self._check_icons = (on, off)             # 必须持有引用，否则图片被回收
        style.element_create(name, "image", off, ("selected", on), sticky="")
        style.layout("TCheckbutton", [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                (name, {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]}),
            ]}),
        ])

    def _init_vars(self) -> None:
        s = self.saved
        self.var_dark = tk.BooleanVar(value=bool(s.get("dark", False)))
        # 裁剪相关
        self.var_crop_mode = tk.StringVar(value=s.get("crop_mode", "矩形"))
        self.var_crop_border = tk.StringVar(value=s.get("crop_border", imaging.BORDER_NONE))
        self.var_crop_border_w = tk.IntVar(value=s.get("crop_border_w", 0))

        # 画布相关（画布尺寸属于文档属性，不记忆）
        self.var_mode = tk.StringVar(value=s.get("mode", imaging.MODE_FREE))
        self.var_bg_kind = tk.StringVar(value=s.get("bg_kind", "透明"))
        self.var_snap = tk.BooleanVar(value=bool(s.get("snap", True)))
        self.var_canvas_w = tk.IntVar(value=self.doc.canvas_w)
        self.var_canvas_h = tk.IntVar(value=self.doc.canvas_h)
        try:
            self.background_color = tuple(int(v) for v in s.get("bg_color", (255, 255, 255)))[:3]
        except (TypeError, ValueError):
            self.background_color = (255, 255, 255)

        # 顺序拼接
        self.var_stitch_dir = tk.StringVar(value=s.get("stitch_dir", imaging.STITCH_HORIZONTAL))
        self.var_stitch_gap = tk.IntVar(value=s.get("stitch_gap", 0))
        self.var_stitch_align = tk.StringVar(value=s.get("stitch_align", imaging.ALIGN_START))

        # 网格
        self.var_grid_rows = tk.IntVar(value=s.get("grid_rows", 2))
        self.var_grid_cols = tk.IntVar(value=s.get("grid_cols", 2))
        self.var_grid_gap = tk.IntVar(value=s.get("grid_gap", 8))
        self.var_grid_cell = tk.StringVar(value=s.get("grid_cell", "自动"))
        self.var_grid_cw = tk.IntVar(value=s.get("grid_cw", 400))
        self.var_grid_ch = tk.IntVar(value=s.get("grid_ch", 400))
        self.var_grid_fit = tk.StringVar(value=s.get("grid_fit", imaging.FIT_CONTAIN))

        # 把界面选项同步进文档，避免「界面显示网格、实际还是自由模式」
        self.doc.mode = self.var_mode.get()
        self.doc.snap = bool(self.var_snap.get())
        self.doc.background = (imaging.TRANSPARENT if self.var_bg_kind.get() == "透明"
                               else (*self.background_color, 255))
        self.doc.stitch_direction = self.var_stitch_dir.get()
        self.doc.stitch_spacing = max(0, int(self.var_stitch_gap.get() or 0))
        self.doc.stitch_align = self.var_stitch_align.get()
        self.doc.grid_rows = max(1, int(self.var_grid_rows.get() or 1))
        self.doc.grid_cols = max(1, int(self.var_grid_cols.get() or 1))
        self.doc.grid_gap = max(0, int(self.var_grid_gap.get() or 0))
        self.doc.grid_fit = self.var_grid_fit.get()
        if self.var_grid_cell.get() == "自定义":
            self.doc.grid_cell = (max(1, int(self.var_grid_cw.get() or 1)),
                                  max(1, int(self.var_grid_ch.get() or 1)))

        # 选中元素属性
        self.var_item_x = tk.DoubleVar(value=0)
        self.var_item_y = tk.DoubleVar(value=0)
        self.var_item_scale = tk.IntVar(value=100)
        self.var_item_rot = tk.IntVar(value=0)
        self.var_item_flip_h = tk.BooleanVar(value=False)
        self.var_item_flip_v = tk.BooleanVar(value=False)
        self.var_item_title = tk.StringVar(value="未选中元素")
        self.var_border = tk.StringVar(value=imaging.BORDER_NONE)
        self.var_border_w = tk.IntVar(value=0)

        self._item_vars = (self.var_item_x, self.var_item_y, self.var_item_scale,
                           self.var_item_rot, self.var_item_flip_h, self.var_item_flip_v)
        for var in self._item_vars:
            var.trace_add("write", self._on_item_var_changed)
        self.var_border.trace_add("write", self._on_border_changed)
        self.var_border_w.trace_add("write", self._on_border_changed)

    # ============================================================ 界面搭建

    def _build_ui(self) -> None:
        top = ttk.Frame(self, style="Bar.TFrame")
        top.pack(fill="x")
        ttk.Button(top, text="导入图片…", command=self.import_images).pack(side="left")
        ttk.Button(top, text="移除选中源图", command=self.remove_source).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="清空源图", command=self.clear_sources).pack(side="left", padx=(6, 0))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(top, text="导出图片…", command=self.open_export).pack(side="left")
        ttk.Label(top, text="Ctrl+Z 撤回 / Ctrl+滚轮缩放 / 中键拖拽平移 / Del 删除选中元素",
                  style="Hint.TLabel").pack(side="right")
        ttk.Checkbutton(top, text="深色模式", variable=self.var_dark,
                        command=self.toggle_theme).pack(side="right", padx=(0, 14))

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, padding=6)
        center = ttk.Frame(paned, padding=(0, 0))
        right = ttk.Frame(paned, padding=6)
        paned.add(left, weight=0)
        paned.add(center, weight=1)
        paned.add(right, weight=0)

        self._build_sources(left)
        self._build_center(center)
        self._build_side(right)

        status = ttk.Frame(self, style="Bar.TFrame")
        status.pack(fill="x", side="bottom")
        self.status = ttk.Label(status, text="", anchor="w")
        self.status.pack(fill="x")

    # ---------------- 左侧：源图列表 ----------------

    def _build_sources(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="源图列表（双击在裁剪区打开）", padding=6)
        box.pack(fill="both", expand=True)

        self.source_list = tk.Listbox(box, width=30, activestyle="none", exportselection=False)
        self.source_list.pack(fill="both", expand=True)
        self.source_list.bind("<Double-Button-1>", lambda e: self.open_current_source())
        self.source_list.bind("<<ListboxSelect>>", lambda e: self.open_current_source())

        hint = ttk.Label(box, text="提示：多边形模式可框出任意形状，\n形状外的区域导出为透明。\n也可以把图片直接拖进窗口。",
                         style="Hint.TLabel", justify="left")
        hint.pack(fill="x", pady=(8, 0))

    # ---------------- 中间：裁剪 / 合成 两个工作区 ----------------

    def _build_center(self, parent: ttk.Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True)

        crop_tab = ttk.Frame(self.notebook, padding=6)
        compose_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(crop_tab, text="  ① 裁剪素材  ")
        self.notebook.add(compose_tab, text="  ② 合成画布  ")

        self._build_crop_tab(crop_tab)
        self._build_compose_tab(compose_tab)

    def _build_crop_tab(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 6))

        ttk.Label(bar, text="裁剪方式").pack(side="left")
        combo = ttk.Combobox(bar, textvariable=self.var_crop_mode, state="readonly",
                             width=8, values=("矩形", "多边形"))
        combo.pack(side="left", padx=(4, 12))
        combo.bind("<<ComboboxSelected>>", lambda e: self.on_crop_mode_changed())

        ttk.Label(bar, text="边框").pack(side="left")
        border = ttk.Combobox(bar, textvariable=self.var_crop_border, state="readonly",
                              width=6, values=imaging.BORDER_OPTIONS)
        border.pack(side="left", padx=(4, 6))
        ttk.Label(bar, text="宽度").pack(side="left")
        ttk.Spinbox(bar, from_=0, to=200, width=5,
                    textvariable=self.var_crop_border_w).pack(side="left", padx=(4, 12))

        ttk.Button(bar, text="加入素材库", command=self.crop_now).pack(side="left")
        ttk.Button(bar, text="清除选区", command=lambda: self.crop_view.clear_selection()).pack(side="left", padx=(6, 12))

        ttk.Button(bar, text="适应窗口", command=lambda: self.crop_view.fit()).pack(side="left")
        ttk.Button(bar, text="－", width=3, command=lambda: self.crop_view.zoom_step(1 / 1.25)).pack(side="left")
        ttk.Button(bar, text="＋", width=3, command=lambda: self.crop_view.zoom_step(1.25)).pack(side="left")
        ttk.Button(bar, text="1:1", command=lambda: self.crop_view.zoom_reset()).pack(side="left", padx=(4, 0))

        self.crop_view = views.CropView(parent, on_piece_created=self.on_piece_created,
                                        push_undo=self.push_undo,
                                        request_crop=self.crop_now)
        self.crop_view.pack(fill="both", expand=True)

    def _build_compose_tab(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 6))

        ttk.Label(bar, text="合并模式").pack(side="left")
        mode = ttk.Combobox(bar, textvariable=self.var_mode, state="readonly", width=9,
                            values=imaging.MODE_OPTIONS)
        mode.pack(side="left", padx=(4, 12))
        mode.bind("<<ComboboxSelected>>", lambda e: self.on_mode_changed())

        ttk.Label(bar, text="间隙 / 背景").pack(side="left")
        bg = ttk.Combobox(bar, textvariable=self.var_bg_kind, state="readonly", width=5,
                          values=("透明", "颜色"))
        bg.pack(side="left", padx=(4, 4))
        bg.bind("<<ComboboxSelected>>", lambda e: self.on_background_changed())
        self.bg_swatch = tk.Button(bar, width=3,
                                   bg=views.rgb_to_hex((*self.background_color, 255)),
                                   command=self.pick_background_color)
        self.bg_swatch.pack(side="left", padx=(0, 12))

        ttk.Checkbutton(bar, text="自动贴紧", variable=self.var_snap,
                        command=self.on_snap_changed).pack(side="left", padx=(0, 12))

        ttk.Label(bar, text="画布").pack(side="left")
        self.spin_cw = ttk.Spinbox(bar, from_=1, to=20000, width=6, textvariable=self.var_canvas_w,
                                   command=self.on_canvas_size_changed)
        self.spin_cw.pack(side="left", padx=(4, 2))
        ttk.Label(bar, text="×").pack(side="left")
        self.spin_ch = ttk.Spinbox(bar, from_=1, to=20000, width=6, textvariable=self.var_canvas_h,
                                   command=self.on_canvas_size_changed)
        self.spin_ch.pack(side="left", padx=(2, 6))
        self.btn_fit_content = ttk.Button(bar, text="贴合内容", command=self.fit_canvas_to_content)
        self.btn_fit_content.pack(side="left", padx=(0, 12))

        ttk.Button(bar, text="适应窗口", command=lambda: self.compose_view.fit()).pack(side="left")
        ttk.Button(bar, text="－", width=3, command=lambda: self.compose_view.zoom_step(1 / 1.25)).pack(side="left")
        ttk.Button(bar, text="＋", width=3, command=lambda: self.compose_view.zoom_step(1.25)).pack(side="left")
        ttk.Button(bar, text="1:1", command=lambda: self.compose_view.zoom_reset()).pack(side="left", padx=(4, 0))

        # 模式专属参数行
        self.mode_bar = ttk.Frame(parent)
        self.mode_bar.pack(fill="x", pady=(0, 6))

        self.stitch_bar = ttk.Frame(self.mode_bar)
        ttk.Label(self.stitch_bar, text="方向").pack(side="left")
        d = ttk.Combobox(self.stitch_bar, textvariable=self.var_stitch_dir, state="readonly", width=6,
                         values=(imaging.STITCH_HORIZONTAL, imaging.STITCH_VERTICAL))
        d.pack(side="left", padx=(4, 12))
        d.bind("<<ComboboxSelected>>", lambda e: self.on_layout_changed())
        ttk.Label(self.stitch_bar, text="间距").pack(side="left")
        g = ttk.Spinbox(self.stitch_bar, from_=0, to=2000, width=6, textvariable=self.var_stitch_gap,
                        command=self.on_layout_changed)
        g.pack(side="left", padx=(4, 12))
        ttk.Label(self.stitch_bar, text="对齐").pack(side="left")
        a = ttk.Combobox(self.stitch_bar, textvariable=self.var_stitch_align, state="readonly", width=6,
                         values=imaging.ALIGN_OPTIONS)
        a.pack(side="left", padx=(4, 12))
        a.bind("<<ComboboxSelected>>", lambda e: self.on_layout_changed())
        ttk.Label(self.stitch_bar, text="素材按素材库顺序依次首尾相接",
                  style="Hint.TLabel").pack(side="left")

        self.grid_bar = ttk.Frame(self.mode_bar)
        ttk.Label(self.grid_bar, text="行").pack(side="left")
        ttk.Spinbox(self.grid_bar, from_=1, to=20, width=4, textvariable=self.var_grid_rows,
                    command=self.on_layout_changed).pack(side="left", padx=(4, 8))
        ttk.Label(self.grid_bar, text="列").pack(side="left")
        ttk.Spinbox(self.grid_bar, from_=1, to=20, width=4, textvariable=self.var_grid_cols,
                    command=self.on_layout_changed).pack(side="left", padx=(4, 8))
        ttk.Label(self.grid_bar, text="间距").pack(side="left")
        ttk.Spinbox(self.grid_bar, from_=0, to=2000, width=5, textvariable=self.var_grid_gap,
                    command=self.on_layout_changed).pack(side="left", padx=(4, 12))
        ttk.Label(self.grid_bar, text="单元").pack(side="left")
        cell = ttk.Combobox(self.grid_bar, textvariable=self.var_grid_cell, state="readonly", width=6,
                            values=("自动", "自定义"))
        cell.pack(side="left", padx=(4, 4))
        cell.bind("<<ComboboxSelected>>", lambda e: self.on_layout_changed())
        ttk.Spinbox(self.grid_bar, from_=1, to=20000, width=6, textvariable=self.var_grid_cw,
                    command=self.on_layout_changed).pack(side="left")
        ttk.Label(self.grid_bar, text="×").pack(side="left")
        ttk.Spinbox(self.grid_bar, from_=1, to=20000, width=6, textvariable=self.var_grid_ch,
                    command=self.on_layout_changed).pack(side="left", padx=(0, 12))
        ttk.Label(self.grid_bar, text="填充").pack(side="left")
        fit = ttk.Combobox(self.grid_bar, textvariable=self.var_grid_fit, state="readonly", width=6,
                           values=imaging.FIT_OPTIONS)
        fit.pack(side="left", padx=(4, 12))
        fit.bind("<<ComboboxSelected>>", lambda e: self.on_layout_changed())
        ttk.Label(self.grid_bar, text="素材多于格子数时自动分页导出",
                  style="Hint.TLabel").pack(side="left")

        self.free_hint = ttk.Label(
            self.mode_bar,
            text="自由模式：把素材拖到画布上随意摆放，靠近其他图片时会自动贴紧。",
            style="Hint.TLabel",
        )

        self.compose_view = views.ComposeView(
            parent, self.doc,
            on_select=self.on_item_selected,
            on_change=self.on_item_moved,
            push_undo=self.push_undo,
        )
        self.compose_view.pack(fill="both", expand=True)

        self._update_mode_bar()

    # ---------------- 右侧：素材库 + 属性 ----------------

    def _build_side(self, parent: ttk.Frame) -> None:
        tray_box = ttk.LabelFrame(parent, text="素材库（裁剪结果）", padding=6)
        tray_box.pack(fill="both", expand=True)

        self.tray = ttk.Treeview(tray_box, show="tree", selectmode="browse", height=9)
        self.tray.column("#0", width=210)
        self.tray.pack(fill="both", expand=True)
        self.tray.bind("<Double-Button-1>", lambda e: self.add_selected_piece())

        btns = ttk.Frame(tray_box)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="添加到画布", command=self.add_selected_piece).pack(side="left")
        ttk.Button(btns, text="全部加入", command=self.add_all_pieces).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="删除素材", command=self.delete_selected_piece).pack(side="left", padx=(6, 0))

        prop = ttk.LabelFrame(parent, text="选中元素属性", padding=6)
        prop.pack(fill="x", pady=(10, 0))
        prop.columnconfigure(1, weight=1)
        prop.columnconfigure(3, weight=1)

        widgets: list = []
        self.prop_widgets = widgets

        ttk.Label(prop, textvariable=self.var_item_title, foreground="#2d8cf0").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        ttk.Label(prop, text="位置 X").grid(row=1, column=0, sticky="w")
        widgets.append(ttk.Spinbox(prop, from_=-20000, to=20000, width=7, textvariable=self.var_item_x))
        widgets[-1].grid(row=1, column=1, sticky="we", padx=(4, 8))
        ttk.Label(prop, text="Y").grid(row=1, column=2, sticky="w")
        widgets.append(ttk.Spinbox(prop, from_=-20000, to=20000, width=7, textvariable=self.var_item_y))
        widgets[-1].grid(row=1, column=3, sticky="we", padx=(4, 0))

        ttk.Label(prop, text="缩放 %").grid(row=2, column=0, sticky="w", pady=(6, 0))
        widgets.append(ttk.Spinbox(prop, from_=1, to=2000, width=7, textvariable=self.var_item_scale))
        widgets[-1].grid(row=2, column=1, sticky="we", padx=(4, 8), pady=(6, 0))
        ttk.Label(prop, text="旋转").grid(row=2, column=2, sticky="w", pady=(6, 0))
        widgets.append(ttk.Combobox(prop, width=5, state="readonly", textvariable=self.var_item_rot,
                                    values=(0, 90, 180, 270)))
        widgets[-1].grid(row=2, column=3, sticky="we", padx=(4, 0), pady=(6, 0))

        flip = ttk.Frame(prop)
        flip.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        widgets.append(ttk.Checkbutton(flip, text="水平翻转", variable=self.var_item_flip_h))
        widgets[-1].pack(side="left")
        widgets.append(ttk.Checkbutton(flip, text="垂直翻转", variable=self.var_item_flip_v))
        widgets[-1].pack(side="left", padx=(10, 0))

        layer = ttk.Frame(prop)
        layer.grid(row=4, column=0, columnspan=4, sticky="we", pady=(8, 0))
        ttk.Button(layer, text="上移一层", command=lambda: self.move_layer(1)).pack(side="left")
        ttk.Button(layer, text="下移一层", command=lambda: self.move_layer(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(layer, text="从画布移除", command=self.remove_item).pack(side="right")

        ttk.Label(prop, text="边框（对该素材的所有摆放生效）",
                  style="Hint.TLabel").grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 2))
        border_row = ttk.Frame(prop)
        border_row.grid(row=6, column=0, columnspan=4, sticky="we")
        widgets.append(ttk.Combobox(border_row, width=6, state="readonly",
                                    textvariable=self.var_border, values=imaging.BORDER_OPTIONS))
        widgets[-1].pack(side="left")
        ttk.Label(border_row, text="宽度").pack(side="left", padx=(8, 4))
        widgets.append(ttk.Spinbox(border_row, from_=0, to=200, width=6, textvariable=self.var_border_w))
        widgets[-1].pack(side="left")

        self._set_property_enabled(False)

    # ============================================================ 源图

    def _init_dnd(self) -> None:
        """注册「把图片拖进窗口」的支持。

        Tk 顶层由 TkTopLevel（外层框架）+ TkChild（内容）两层 HWND 组成，
        winfo_id() 拿到的是铺满客户区的 TkChild，拖到窗口里的任意位置都会落到它上面。
        """
        self._drop_queue: queue.Queue = queue.Queue()

        def on_drop(files) -> None:
            # 回调由 Windows 消息钩子触发，这里只入队，回主线程后再处理
            self._drop_queue.put(list(files))

        try:
            import windnd
            windnd.hook_dropfiles(self, func=on_drop, force_unicode=True)
        except Exception:
            self.dnd_hint = ""
            return
        self.dnd_hint = "　·　可直接把图片拖进窗口"
        self._poll_id = self.after(120, self._poll_drops)
        self.update_status()

    def _poll_drops(self) -> None:
        try:
            while True:
                files = self._drop_queue.get_nowait()
                self.add_image_paths(files)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._poll_id = self.after(120, self._poll_drops)

    def import_images(self) -> None:
        paths = filedialog.askopenfilenames(title="选择图片", filetypes=OPEN_TYPES, parent=self)
        if not paths:
            return
        self.add_image_paths(paths)

    def add_image_paths(self, paths) -> None:
        """把一组图片路径导入为源图（文件对话框与拖拽共用）。"""
        failed = []
        loaded: list[model.SourceImage] = []
        for path in paths:
            if os.path.isdir(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in IMAGE_EXT:
                continue
            try:
                img = imaging.load_image(path)
            except Exception as exc:  # 个别文件损坏不影响其它图片
                failed.append(f"{os.path.basename(path)}：{exc}")
                continue
            loaded.append(model.SourceImage(image=img, name=os.path.basename(path), path=path))

        if loaded:
            self.push_undo()
            self.sources.extend(loaded)
            self.refresh_sources()
            self.source_list.selection_clear(0, "end")
            self.source_list.selection_set(max(0, len(self.sources) - len(loaded)))
            self.open_current_source()
            self.update_status()
        if failed:
            messagebox.showwarning("部分图片导入失败", "\n".join(failed), parent=self)

    def current_source(self) -> model.SourceImage | None:
        sel = self.source_list.curselection()
        if not sel:
            return None
        index = int(sel[0])
        if 0 <= index < len(self.sources):
            return self.sources[index]
        return None

    def open_current_source(self) -> None:
        if self._restoring:
            return
        source = self.current_source()
        if source is None:
            return
        self.crop_view.set_image(source.image, source.name)
        self.notebook.select(0)
        self.update_status()

    def remove_source(self) -> None:
        sel = self.source_list.curselection()
        if not sel:
            return
        index = int(sel[0])
        self.push_undo()
        if 0 <= index < len(self.sources):
            self.sources.pop(index)
        self.refresh_sources()
        if self.sources:
            self.source_list.selection_set(min(index, len(self.sources) - 1))
            self.open_current_source()
        else:
            self.crop_view.set_image(None)
        self.update_status()

    def clear_sources(self) -> None:
        if self.sources and not messagebox.askyesno("清空源图", "确定移除所有已导入的源图吗？", parent=self):
            return
        self.push_undo()
        self.sources.clear()
        self.refresh_sources()
        self.crop_view.set_image(None)
        self.update_status()

    def refresh_sources(self) -> None:
        self.source_list.delete(0, "end")
        for source in self.sources:
            self.source_list.insert("end", f" {source.name}  ({source.image.width}×{source.image.height})")

    # ============================================================ 裁剪

    def crop_now(self) -> None:
        try:
            width = max(0, int(self.var_crop_border_w.get()))
        except (tk.TclError, ValueError):
            width = 0
        self.crop_view.set_border(self.var_crop_border.get(), width)
        self.crop_view.crop_now()

    def on_crop_mode_changed(self) -> None:
        self.crop_view.set_mode(self.var_crop_mode.get())
        self.update_status()

    def on_piece_created(self, piece: model.CropPiece) -> None:
        self.pieces.append(piece)
        self.refresh_tray()
        self.update_status()

    def refresh_tray(self) -> None:
        self.tray.delete(*self.tray.get_children())
        self._tray_thumbs.clear()
        for piece in self.pieces:
            w, h = piece.size
            photo = self._tray_photo(piece)
            self._tray_thumbs[piece.pid] = photo
            self.tray.insert("", "end", iid=str(piece.pid), image=photo,
                             text=f" {piece.name}  ({w}×{h})")

    def _tray_photo(self, piece: model.CropPiece) -> ImageTk.PhotoImage:
        """缩略图统一缩放到同尺寸方框内居中，横图竖图都能对齐。"""
        thumb = piece.render().copy()
        thumb.thumbnail((THUMB, THUMB), Image.LANCZOS)
        box = Image.new("RGB", (THUMB, THUMB), self.palette.get("field", "#ffffff"))
        box.paste(thumb, ((THUMB - thumb.width) // 2, (THUMB - thumb.height) // 2), thumb)
        return ImageTk.PhotoImage(box)

    def _selected_piece(self) -> model.CropPiece | None:
        sel = self.tray.selection()
        if not sel:
            return None
        pid = int(sel[0])
        return next((p for p in self.pieces if p.pid == pid), None)

    def add_piece_to_canvas(self, piece: model.CropPiece, cascade: int = 0) -> model.PlacedItem:
        item = model.PlacedItem(piece=piece)
        w, h = item.size
        item.x = max(0, (self.doc.canvas_w - w) / 2) + cascade * 24
        item.y = max(0, (self.doc.canvas_h - h) / 2) + cascade * 24
        self.doc.add(item)
        return item

    def add_selected_piece(self) -> None:
        piece = self._selected_piece()
        if piece is None:
            messagebox.showinfo("提示", "请先在素材库里选中一个素材。", parent=self)
            return
        self.push_undo()
        item = self.add_piece_to_canvas(piece, len(self.doc.items) % 6)
        self.refresh_canvas()
        self.compose_view.select(item, raise_to_top=True)
        self.notebook.select(1)
        self.update_status()

    def add_all_pieces(self) -> None:
        if not self.pieces:
            messagebox.showinfo("提示", "素材库还是空的，先在「① 裁剪素材」里裁出素材。", parent=self)
            return
        self.push_undo()
        cascade = 0
        last = None
        for piece in self.pieces:
            last = self.add_piece_to_canvas(piece, cascade)
            cascade = (cascade + 1) % 6
        self.refresh_canvas(refit=self.doc.mode != imaging.MODE_FREE)
        if last is not None:
            self.compose_view.select(last, raise_to_top=True)
        self.notebook.select(1)
        self.update_status()

    def delete_selected_piece(self) -> None:
        piece = self._selected_piece()
        if piece is None:
            return
        if not messagebox.askyesno("删除素材", f"删除素材「{piece.name}」？\n画布上使用它的元素也会一并移除。",
                                   parent=self):
            return
        self.push_undo()
        for item in [i for i in self.doc.items if i.piece is piece]:
            self.doc.remove(item)
        self.pieces.remove(piece)
        self.refresh_tray()
        self.refresh_canvas()
        self.update_status()

    # ============================================================ 画布

    def refresh_canvas(self, refit: bool = False) -> None:
        self.compose_view.refresh(refit=refit)
        self.update_status()

    def on_mode_changed(self) -> None:
        mode = self.var_mode.get()
        if mode == self.doc.mode:
            return
        self.push_undo()
        self.doc.mode = mode
        self._update_mode_bar()
        self.compose_view.select(None)
        self.refresh_canvas(refit=True)

    def _update_mode_bar(self) -> None:
        mode = self.var_mode.get()
        for widget in (self.stitch_bar, self.grid_bar, self.free_hint):
            widget.pack_forget()
        if mode == imaging.MODE_STITCH:
            self.stitch_bar.pack(side="left")
        elif mode == imaging.MODE_GRID:
            self.grid_bar.pack(side="left")
        else:
            self.free_hint.pack(side="left")
        free = mode == imaging.MODE_FREE
        for widget in (self.spin_cw, self.spin_ch, self.btn_fit_content):
            widget.configure(state="normal" if free else "disabled")

    def on_background_changed(self) -> None:
        if self.var_bg_kind.get() == "透明":
            target = imaging.TRANSPARENT
        else:
            target = (*self.background_color, 255)
        if target == self.doc.background:
            return
        self.push_undo()
        self.doc.background = target
        self.refresh_canvas()

    def pick_background_color(self) -> None:
        rgb, _ = colorchooser.askcolor(color=views.rgb_to_hex((*self.background_color, 255)),
                                       title="选择间隙 / 背景颜色", parent=self)
        if not rgb:
            return
        self.background_color = tuple(int(v) for v in rgb)
        self.bg_swatch.configure(bg=views.rgb_to_hex((*self.background_color, 255)))
        self.var_bg_kind.set("颜色")
        self.on_background_changed()

    def on_snap_changed(self) -> None:
        self.doc.snap = bool(self.var_snap.get())
        self.update_status()

    def on_canvas_size_changed(self) -> None:
        try:
            w = max(1, int(self.var_canvas_w.get()))
            h = max(1, int(self.var_canvas_h.get()))
        except (tk.TclError, ValueError):
            return
        if (w, h) == (self.doc.canvas_w, self.doc.canvas_h):
            return
        self.push_undo()
        self.doc.canvas_w, self.doc.canvas_h = w, h
        self.refresh_canvas()

    def fit_canvas_to_content(self) -> None:
        if not self.doc.items:
            return
        self.push_undo()
        if self.doc.mode == imaging.MODE_FREE:
            bounds = self.doc.content_bounds()
            if bounds is None:
                return
            x0, y0, x1, y1 = bounds
            margin = 20
            for item in self.doc.items:
                item.x += margin - x0
                item.y += margin - y0
            self.doc.canvas_w = int(x1 - x0 + margin * 2)
            self.doc.canvas_h = int(y1 - y0 + margin * 2)
            self.var_canvas_w.set(self.doc.canvas_w)
            self.var_canvas_h.set(self.doc.canvas_h)
        else:
            size, _ = self.doc.preview()
            self.doc.canvas_w, self.doc.canvas_h = int(size[0]), int(size[1])
            self.var_canvas_w.set(self.doc.canvas_w)
            self.var_canvas_h.set(self.doc.canvas_h)
        self.refresh_canvas(refit=True)

    def on_layout_changed(self) -> None:
        try:
            direction = self.var_stitch_dir.get()
            spacing = max(0, int(self.var_stitch_gap.get()))
            align = self.var_stitch_align.get()
            rows = max(1, int(self.var_grid_rows.get()))
            cols = max(1, int(self.var_grid_cols.get()))
            gap = max(0, int(self.var_grid_gap.get()))
            fit = self.var_grid_fit.get()
            if self.var_grid_cell.get() == "自定义":
                cell = (max(1, int(self.var_grid_cw.get())), max(1, int(self.var_grid_ch.get())))
            else:
                cell = None
        except (tk.TclError, ValueError):
            return
        doc = self.doc
        if (direction, spacing, align, rows, cols, gap, fit, cell) == (
                doc.stitch_direction, doc.stitch_spacing, doc.stitch_align,
                doc.grid_rows, doc.grid_cols, doc.grid_gap, doc.grid_fit, doc.grid_cell):
            return
        self.push_undo()
        doc.stitch_direction = direction
        doc.stitch_spacing = spacing
        doc.stitch_align = align
        doc.grid_rows = rows
        doc.grid_cols = cols
        doc.grid_gap = gap
        doc.grid_fit = fit
        doc.grid_cell = cell
        self.refresh_canvas()

    def on_item_moved(self) -> None:
        self._sync_item_fields()
        self.update_status()

    def on_item_selected(self, item: model.PlacedItem | None) -> None:
        self._set_property_enabled(item is not None)
        if item is None:
            self.var_item_title.set("未选中元素")
            self._panel_lock = True
            for var in self._item_vars:
                var.set(0)
            self.var_item_scale.set(100)
            self.var_item_flip_h.set(False)
            self.var_item_flip_v.set(False)
            self._panel_lock = False
            return
        self.var_item_title.set(f"{item.piece.name} （ID {item.iid}）")
        self._panel_lock = True
        self.var_item_x.set(round(item.x, 1))
        self.var_item_y.set(round(item.y, 1))
        self.var_item_scale.set(int(round(item.scale * 100)))
        self.var_item_rot.set(int(item.rot) % 360)
        self.var_item_flip_h.set(bool(item.flip_h))
        self.var_item_flip_v.set(bool(item.flip_v))
        self.var_border.set(item.piece.border)
        self.var_border_w.set(int(item.piece.border_width))
        self._panel_lock = False

    def _sync_item_fields(self) -> None:
        item = self.compose_view.selected
        if item is None:
            return
        self._panel_lock = True
        self.var_item_x.set(round(item.x, 1))
        self.var_item_y.set(round(item.y, 1))
        self._panel_lock = False

    def _on_item_var_changed(self, *_args) -> None:
        if self._panel_lock:
            return
        item = self.compose_view.selected
        if item is None:
            return
        try:
            x = float(self.var_item_x.get())
            y = float(self.var_item_y.get())
            scale = max(0.01, float(self.var_item_scale.get()) / 100.0)
            rot = int(self.var_item_rot.get()) % 360
            flip_h = bool(self.var_item_flip_h.get())
            flip_v = bool(self.var_item_flip_v.get())
        except (tk.TclError, ValueError):
            return
        if (abs(x - item.x) < 0.05 and abs(y - item.y) < 0.05
                and abs(scale - item.scale) < 1e-6 and rot == int(item.rot) % 360
                and flip_h == bool(item.flip_h) and flip_v == bool(item.flip_v)):
            return                     # 数值没变，不入撤回栈
        self.push_undo()
        item.x, item.y = x, y
        item.scale, item.rot = scale, rot
        item.flip_h, item.flip_v = flip_h, flip_v
        self.compose_view.redraw()
        self.update_status()

    def _on_border_changed(self, *_args) -> None:
        if self._panel_lock:
            return
        piece = None
        item = self.compose_view.selected
        if item is not None:
            piece = item.piece
        else:
            piece = self._selected_piece()
        if piece is None:
            return
        try:
            border = self.var_border.get()
            width = max(0, int(self.var_border_w.get()))
        except (tk.TclError, ValueError):
            return
        if (border, width) == (piece.border, int(piece.border_width)):
            return
        self.push_undo()
        piece.border = border
        piece.border_width = width
        self.refresh_tray()
        self.refresh_canvas()

    def move_layer(self, delta: int) -> None:
        item = self.compose_view.selected
        if item is None:
            return
        items = self.doc.items
        i = items.index(item)
        j = max(0, min(len(items) - 1, i + delta))
        if i == j:                     # 已经在最上层 / 最下层，视为无操作
            return
        self.push_undo()
        self.doc.move_layer(item, delta)
        self.compose_view.redraw()
        self.update_status()

    def remove_item(self) -> None:
        self.compose_view.delete_selected()

    def _set_property_enabled(self, enabled: bool) -> None:
        for widget in self.prop_widgets:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="readonly" if enabled else "disabled")
                else:
                    widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    # ============================================================ 状态栏

    def update_status(self) -> None:
        piece_count = len(self.pieces)
        item_count = len(self.doc.items)
        pages = len(self.doc.pages())
        bg = "透明" if self.doc.background is None else "颜色"
        snap = "开" if self.doc.snap else "关"
        undo = f" · 可撤回 {len(self.history)} 步" if self.history else ""
        crop_hint = {
            "多边形": "裁剪：点起点 / 右键闭合，再点一下完成",
            "套索": "裁剪：按住左键描边，松开闭合，再点一下完成",
        }.get(self.var_crop_mode.get(), "裁剪：拖拽框选后点「加入素材库」")
        self.status.configure(
            text=f"源图 {len(self.sources)} 张 · 素材 {piece_count} 个 · 画布元素 {item_count} 个 · "
                 f"模式 {self.doc.mode} · 画布 {self.doc.canvas_w}×{self.doc.canvas_h} · "
                 f"背景/间隙 {bg} · 自动贴紧 {snap} · 成品 {pages} 页{undo} · {crop_hint}{self.dnd_hint}"
        )

    # ============================================================ 导出

    def open_export(self) -> None:
        if not self.doc.items and not self.pieces:
            messagebox.showinfo("提示", "还没有可导出的内容。", parent=self)
            return
        ExportDialog(self, self.doc, self.pieces, self.last_export_dir, self.run_export)

    def run_export(self, opts: dict) -> list[str]:
        folder = opts["folder"]
        prefix = opts["prefix"] or "合成"
        fmt = opts["format"]
        quality = int(opts["quality"])
        ext = ".jpg" if fmt == "JPEG" else ".webp" if fmt == "WEBP" else ".png"
        os.makedirs(folder, exist_ok=True)
        saved: list[str] = []

        if opts["composite"] and self.doc.items:
            pages = self.doc.pages()
            for index, (size, placements) in enumerate(pages, 1):
                image = imaging.render_composition(size, placements, self.doc.background)
                suffix = "" if len(pages) == 1 else f"_{index:02d}"
                path = os.path.join(folder, f"{prefix}_成品{suffix}{ext}")
                imaging.save_image(image, path, fmt, quality)
                saved.append(path)

        if opts["pieces"] and self.pieces:
            for index, piece in enumerate(self.pieces, 1):
                path = os.path.join(folder, f"{prefix}_素材{index:02d}{ext}")
                imaging.save_image(piece.render().copy(), path, fmt, quality)
                saved.append(path)

        return saved


# ================================================================ 导出对话框

class ExportDialog(tk.Toplevel):
    def __init__(self, master: App, doc: model.Composition, pieces, folder: str, on_export):
        super().__init__(master)
        self.title("导出图片")
        self.transient(master)
        self.resizable(False, False)
        self.on_export = on_export
        self.doc = doc
        self.pieces = pieces

        self.var_folder = tk.StringVar(value=folder)
        self.var_prefix = tk.StringVar(value="合成")
        self.var_composite = tk.BooleanVar(value=bool(doc.items))
        self.var_pieces = tk.BooleanVar(value=bool(pieces))
        self.var_format = tk.StringVar(value="PNG")
        self.var_quality = tk.DoubleVar(value=95)
        self.var_open = tk.BooleanVar(value=True)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="输出文件夹").grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.var_folder, width=46).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(body, text="浏览…", command=self.choose_folder).grid(row=0, column=2)

        ttk.Label(body, text="文件名前缀").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(body, textvariable=self.var_prefix, width=46).grid(
            row=1, column=1, sticky="we", padx=6, pady=(8, 0))

        content = ttk.LabelFrame(body, text="导出内容", padding=8)
        content.grid(row=2, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Checkbutton(content, text=f"成品图（{len(doc.pages())} 页）",
                        variable=self.var_composite).pack(anchor="w")
        ttk.Checkbutton(content, text=f"裁剪后的素材（{len(pieces)} 个）",
                        variable=self.var_pieces).pack(anchor="w")

        ttk.Label(body, text="图片格式").grid(row=3, column=0, sticky="w", pady=(10, 0))
        combo = ttk.Combobox(body, state="readonly", width=10, textvariable=self.var_format,
                             values=("PNG", "JPEG", "WEBP"))
        combo.grid(row=3, column=1, sticky="w", padx=6, pady=(10, 0))
        combo.bind("<<ComboboxSelected>>", lambda e: self._sync_quality())

        ttk.Label(body, text="质量").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.quality = ttk.Scale(body, from_=1, to=100, orient="horizontal",
                                 variable=self.var_quality, length=220)
        self.quality.grid(row=4, column=1, sticky="we", padx=6, pady=(8, 0))
        self.quality_label = ttk.Label(body, text="95")
        self.quality_label.grid(row=4, column=2, sticky="w")
        self.var_quality.trace_add("write", lambda *a: self.quality_label.configure(
            text=str(int(float(self.var_quality.get())))))

        ttk.Label(body, text="PNG 保留透明通道；JPEG 会把透明区域压成白底。",
                  style="Hint.TLabel").grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Checkbutton(body, text="导出后打开文件夹", variable=self.var_open).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        actions = ttk.Frame(body)
        actions.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="导出", command=self.do_export).pack(side="right")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        self._sync_quality()
        self.grab_set()
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _sync_quality(self) -> None:
        state = "normal" if self.var_format.get() in ("JPEG", "WEBP") else "disabled"
        self.quality.configure(state=state)

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择导出文件夹",
                                         initialdir=self.var_folder.get() or None, parent=self)
        if folder:
            self.var_folder.set(folder)

    def do_export(self) -> None:
        folder = self.var_folder.get().strip()
        if not folder:
            messagebox.showwarning("无法导出", "请选择导出文件夹。", parent=self)
            return
        if not self.var_composite.get() and not self.var_pieces.get():
            messagebox.showwarning("无法导出", "请至少勾选「成品图」或「裁剪后的素材」。", parent=self)
            return
        opts = {
            "folder": folder,
            "prefix": self.var_prefix.get().strip(),
            "composite": bool(self.var_composite.get()),
            "pieces": bool(self.var_pieces.get()),
            "format": self.var_format.get(),
            "quality": int(float(self.var_quality.get())),
        }
        try:
            saved = self.on_export(opts)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        master = self.master
        master.last_export_dir = folder
        self.destroy()
        if not saved:
            messagebox.showinfo("导出完成", "没有可导出的内容。", parent=master)
            return
        messagebox.showinfo("导出完成", f"已导出 {len(saved)} 个文件到：\n{folder}", parent=master)
        if self.var_open.get():
            try:
                os.startfile(folder)  # type: ignore[attr-defined]
            except Exception:
                pass