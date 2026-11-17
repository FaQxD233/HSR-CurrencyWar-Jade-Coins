"""双击启动「翡翠刷钱助手」图形界面（不会弹出黑色命令行窗口）。

电脑里装了多个 Python 时，双击可能被交给一个没装依赖的 Python。这里先检查依赖：缺了就从系统登记的
Python 里找一个依赖齐全的重新打开；都不行就弹窗说明缺什么。本文件只用标准库，并兼容较老的 Python。
"""
import ctypes
import importlib.util
import os
import subprocess
import sys
import traceback
import winreg
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "logs" / "gui.log"
TITLE = "翡翠刷钱助手"
REQUIRED = ("keyboard", "rapidocr_onnxruntime", "cv2", "numpy", "PIL", "win32api")
PIP_NAMES = "keyboard rapidocr-onnxruntime opencv-python numpy pillow pywin32"


def record(text):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def fail(text):
    record(text)
    ctypes.windll.user32.MessageBoxW(None, f"{text}\n\n详细信息已记录到 {LOG}", TITLE, 0x10 | 0x10000 | 0x40000)


def missing():
    return [name for name in REQUIRED if importlib.util.find_spec(name) is None]


def registered_pythons():
    """注册表里登记的 Python（当前用户和全机安装），只保留确实存在的 pythonw.exe。"""
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            core = winreg.OpenKey(hive, r"Software\Python\PythonCore")
        except OSError:
            continue
        with core:
            for index in range(winreg.QueryInfoKey(core)[0]):
                try:
                    with winreg.OpenKey(core, winreg.EnumKey(core, index) + r"\InstallPath") as key:
                        found.append(Path(winreg.QueryValue(key, None)) / "pythonw.exe")
                except OSError:
                    continue
    # 不用 Path.exists()：登记的路径若在已断开的装入点上，它会直接抛异常；os.path.isfile 只返回 False
    return [path for path in found if os.path.isfile(str(path))]


def has_dependencies(pythonw):
    check = f"import importlib.util as u, sys; sys.exit(any(u.find_spec(n) is None for n in {REQUIRED!r}))"
    try:
        result = subprocess.run([str(pythonw.with_name("python.exe")), "-c", check], capture_output=True,
                                timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def relaunch_with_suitable_python():
    current = Path(sys.executable).resolve().parent  # 同一安装目录里的 python.exe 和 pythonw.exe 算同一个
    for pythonw in registered_pythons():
        if pythonw.resolve().parent != current and has_dependencies(pythonw):
            record(f"当前 Python（{sys.executable}）缺少依赖，改用 {pythonw} 重新打开")
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # 让新窗口能显示到最前面
            subprocess.Popen([str(pythonw), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(HERE))
            return True
    return False


def main():
    try:
        start()
    except BaseException:  # 没有控制台，任何意外都必须落到日志和弹窗里
        fail(f"启动出错（{sys.executable}）：\n{traceback.format_exc()}")


def start():
    lacking = missing()
    if lacking:
        if not relaunch_with_suitable_python():
            pip = Path(sys.executable).with_name("python.exe")
            fail(f"当前 Python（{sys.executable}，版本 {sys.version.split()[0]}）缺少：{'、'.join(lacking)}，"
                 f"电脑里也没找到装好这些依赖的 Python。\n请在命令行运行：\n\"{pip}\" -m pip install {PIP_NAMES}")
        return
    try:
        import gui
    except Exception:
        fail(f"界面模块导入失败（{sys.executable}）：\n{traceback.format_exc()}")
        return
    gui.main()


if __name__ == "__main__":
    main()
