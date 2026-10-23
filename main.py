"""图片裁剪合并工具 —— 启动入口。

用法：双击 图片自由裁剪.exe，或双击 启动.bat / 执行 python main.py
"""
import os
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        message = ("缺少 Pillow 依赖，请先安装：\n\n"
                   "    python -m pip install Pillow\n")
        try:
            import tkinter.messagebox as mb
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            mb.showerror("缺少依赖", message)
            root.destroy()
        except Exception:
            print(message)
        return

    from app import App
    App().mainloop()


if __name__ == "__main__":
    main()