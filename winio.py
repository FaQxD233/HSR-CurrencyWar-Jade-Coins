"""Windows 平台层：DPI 感知、管理员权限、游戏窗口定位、截图、鼠标点击。

这里只处理屏幕物理像素坐标，不含任何游戏逻辑。
"""
import ctypes
import os
import subprocess
import sys
import time
import winsound
from contextlib import contextmanager

import pywintypes
import win32api
import win32con
import win32event
import win32gui
import win32process
from PIL import Image, ImageGrab
from win32comext.shell import shell, shellcon

UNITY_WINDOW_CLASS = "UnityWndClass"
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def enable_dpi_awareness() -> None:
    """声明 DPI 感知，让截图和点击都使用物理像素（系统缩放 125% 时不会错位）。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
    except AttributeError:  # Windows 10 1703 之前的系统
        ctypes.windll.user32.SetProcessDPIAware()


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _self_command() -> tuple[str, list[str]]:
    """重新运行自己用的程序和参数：Nuitka 打包的 exe 直接运行 exe 本身，脚本则用当前解释器运行。"""
    if "__compiled__" in globals():
        return os.path.abspath(sys.argv[0]), sys.argv[1:]
    return sys.executable, [os.path.abspath(sys.argv[0]), *sys.argv[1:]]


def relaunch_as_admin(console: bool = True, owner: int = 0):
    """以管理员身份重新运行当前脚本，返回新进程的句柄；用户拒绝或启动失败时返回 None。

    console=True：在新的控制台窗口里运行，脚本结束后窗口保留，方便看输出；
    console=False：直接用当前解释器运行（图形界面由 pythonw 启动，不会弹出黑色窗口）。
    owner：发起请求的窗口。由前台窗口发起时 UAC 确认框才会弹到最前面，否则可能只在任务栏闪烁。
    """
    program, args = _self_command()
    if console:
        python = subprocess.list2cmdline([program, *args])
        # 提权后的 cmd 会忽略工作目录，所以先 cd 回来；cmd /k 会去掉首尾各一个引号，整条命令外面再包一层
        command = f'cd /d "{os.getcwd()}" && {python}'
        file, parameters = "cmd.exe", f'/k "{command}"'
    else:
        file, parameters = program, subprocess.list2cmdline(args)
    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | shellcon.SEE_MASK_FLAG_NO_UI,
                                    hwnd=owner, lpVerb="runas", lpFile=file, lpParameters=parameters,
                                    lpDirectory=os.getcwd(), nShow=win32con.SW_SHOWNORMAL)
    except pywintypes.error:  # 用户点了「否」（错误码 1223）或启动失败
        return None
    return info["hProcess"]


def process_exited(process) -> bool:
    return win32event.WaitForSingleObject(process, 0) == win32event.WAIT_OBJECT_0


def process_has_window(process, title: str) -> bool:
    """该进程是否已经显示出标题为 title 的顶层窗口。"""
    pid = win32process.GetProcessId(process)
    return any(text == title and win32process.GetWindowThreadProcessId(hwnd)[1] == pid
               for hwnd, text, _ in _visible_windows())


def message_box(title: str, text: str) -> None:
    """系统错误提示框（置顶、抢到前台）。不依赖 tkinter，界面本身起不来时也能显示。"""
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 | 0x10000 | 0x40000)  # 错误图标 | 前台 | 置顶


def _visible_windows() -> list[tuple[int, str, str]]:
    """所有可见顶层窗口：(句柄, 标题, 窗口类名)。"""
    windows = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            windows.append((hwnd, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd)))
        return True

    win32gui.EnumWindows(visit, None)
    return windows


def find_window(titles: list[str]) -> int | None:
    """优先按标题完全匹配；否则只在 Unity 窗口里按标题包含匹配，避免误中标题含游戏名的浏览器窗口。"""
    windows = _visible_windows()
    exact = [hwnd for hwnd, title, _ in windows if title in titles]
    partial = [hwnd for hwnd, title, cls in windows
               if cls == UNITY_WINDOW_CLASS and any(t in title for t in titles)]
    found = exact or partial
    return found[0] if found else None


def unity_window_titles() -> list[str]:
    """所有可见 Unity 窗口的标题，找不到游戏窗口时用来提示用户。"""
    return [title for _, title, cls in _visible_windows() if cls == UNITY_WINDOW_CLASS]


def window_exists(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))


def window_title(hwnd: int) -> str:
    return win32gui.GetWindowText(hwnd)


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区（不含标题栏和边框）在屏幕上的位置：(left, top, width, height)。"""
    _, _, width, height = win32gui.GetClientRect(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return left, top, width, height


def grab(rect: tuple[int, int, int, int]) -> Image.Image:
    left, top, width, height = rect
    return ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True).convert("RGB")


def is_foreground(hwnd: int) -> bool:
    return win32gui.GetForegroundWindow() == hwnd and not win32gui.IsIconic(hwnd)


def cursor_pos() -> tuple[int, int]:
    return win32api.GetCursorPos()


def move(x: int, y: int) -> None:
    try:
        win32api.SetCursorPos((x, y))
    except pywintypes.error as error:
        raise RuntimeError("系统拒绝移动鼠标：远程桌面断开、锁屏、UAC 弹窗或受限沙箱中运行时都会这样") from error


def click(x: int, y: int, hold: float = 0.06) -> None:
    """移动、按下、抬起之间都留出间隔：游戏每帧才读一次输入，
    抬起后立刻把鼠标移走，游戏可能在别处读到抬起，这次点击就不算数。"""
    move(x, y)
    time.sleep(0.05)
    with _left_button():
        time.sleep(hold)


def drag(start: tuple[int, int], end: tuple[int, int], duration: float = 0.5, hold: float = 0.1) -> None:
    """按住左键从起点匀速拖到终点（约每秒 60 步），到终点停一下再松开。
    参数与 March7thAssistant 在货币战争里实测可用的拖动一致。"""
    (x1, y1), (x2, y2) = start, end
    move(x1, y1)
    time.sleep(0.05)
    with _left_button():
        steps = max(1, round(duration * 60))
        began = time.perf_counter()
        for step in range(1, steps + 1):
            progress = step / steps
            move(round(x1 + (x2 - x1) * progress), round(y1 + (y2 - y1) * progress))
            remaining = began + duration * progress - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        time.sleep(hold)


@contextmanager
def _left_button():
    """按下左键；无论中途是否出错或被 Ctrl+C 打断，最后都会松开，并停留片刻再交还控制。"""
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    try:
        yield
    finally:
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
    time.sleep(0.08)


def beep() -> None:
    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
