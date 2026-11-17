"""真实输入测试：会移动鼠标并点击一个临时窗口，设置环境变量 JADE_LIVE_INPUT=1 才运行。

验证 DPI 感知、客户区定位、设计坐标换算和点击在本机上是一致的（不需要打开游戏）。
"""
import os
import time
import tkinter as tk

import pytest
import win32gui

import vision
import winio

pytestmark = pytest.mark.skipif(os.environ.get("JADE_LIVE_INPUT") != "1",
                                reason="会移动鼠标并点击，设置 JADE_LIVE_INPUT=1 才运行")

TITLE = "jade-farm-input-test"


def pump(root, seconds):
    for _ in range(int(seconds / 0.02)):
        root.update()
        time.sleep(0.02)


def test_click_lands_on_widget():
    winio.enable_dpi_awareness()
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("640x360+300+300")
    root.attributes("-topmost", True)
    clicked = []
    button = tk.Button(root, text="hit", command=lambda: clicked.append(True))
    button.place(relx=0.7, rely=0.6, width=80, height=40)
    pump(root, 0.3)
    try:
        hwnd = winio.find_window([TITLE])
        assert hwnd, "没找到测试窗口"
        rect = winio.client_rect(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(button.winfo_id())
        center = ((left + right) / 2 - rect[0], (top + bottom) / 2 - rect[1])  # 按钮中心的客户区坐标
        design = (center[0] * vision.DESIGN_SIZE[0] / rect[2], center[1] * vision.DESIGN_SIZE[1] / rect[3])
        winio.click(*vision.to_screen(design, rect))
        pump(root, 0.5)
    finally:
        root.destroy()
    assert clicked, "点击没有落在按钮上"
