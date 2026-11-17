"""图形界面：双击「翡翠刷钱助手.pyw」启动。

界面只负责设置、开始 / 暂停 / 停止、诊断和显示日志；刷钱逻辑全部复用 jade_farm.Farm（在后台线程运行）。
启动时先显示窗口，再由窗口申请管理员权限：由前台窗口发起，UAC 确认框才会弹到最前面。
运行日志同时写进 logs/gui.log，启动失败的原因也记在那里。
"""
import logging
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from logging.handlers import RotatingFileHandler
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import jade_farm
import vision
import winio
from capture import report, save_shot
from jade_farm import (CONFIG_PATH, DEBUG_DIR, MAX_BUYS_PER_VISIT, ROOT, Controls, Farm, Game, SetupError, Stop,
                       log, logger)

TITLE = "翡翠刷钱助手"
LOG_PATH = ROOT / "logs" / "gui.log"
FONT = ("Microsoft YaHei UI", 10)
POLL_MS = 150          # 界面刷新间隔（毫秒）
DIAGNOSE_WAIT = 5      # 诊断前等待切回游戏的秒数
ELEVATE_DELAY_MS = 600  # 窗口显示出来后再申请管理员权限
MAX_LOG_LINES = 2000


class QueueHandler(logging.Handler):
    """把日志放进队列，由界面线程取出显示（tkinter 只能在主线程操作）。"""

    def __init__(self, sink: queue.Queue):
        super().__init__()
        self.sink = sink
        self.setFormatter(jade_farm.LOG_FORMAT)

    def emit(self, record):
        self.sink.put(self.format(record))


class App:
    """主窗口。find_game / make_game / load_ocr / controls / sleep / is_admin / relaunch 可替换，
    便于不开游戏、不弹 UAC 也能测试。"""

    def __init__(self, root: tk.Tk, *, config_path=CONFIG_PATH, find_game=jade_farm.find_game, make_game=Game,
                 load_ocr=vision.Ocr, controls: Controls | None = None, debug_dir=DEBUG_DIR, sleep=time.sleep,
                 is_admin=winio.is_admin, relaunch=winio.relaunch_as_admin):
        self.root = root
        self.config_path = config_path
        self.find_game = find_game
        self.make_game = make_game
        self.debug_dir = debug_dir
        self.sleep = sleep
        self.admin = is_admin()
        self.relaunch = relaunch
        self.elevated = None    # 管理员权限新进程的句柄：确认它的窗口出现之前，保留当前窗口
        self.ocr = None
        self.ocr_error = None
        self.farm: Farm | None = None
        self.thread: threading.Thread | None = None
        self.started_at = self.ended_at = None
        self.diagnosing = False
        self.lines = queue.Queue()
        self.handler = QueueHandler(self.lines)
        logger.addHandler(self.handler)

        try:
            self.config = jade_farm.load_config(config_path)
        except SetupError as error:
            self.config = dict(jade_farm.DEFAULTS)
            log(f"{error}。先用默认设置，点开始时会覆盖保存")
        self.controls = controls or Controls(self.config["stop_key"], self.config["pause_key"])

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = lambda *exc: logger.error("界面出错：\n" + "".join(traceback.format_exception(*exc)))
        threading.Thread(target=self._load_ocr, args=(load_ocr,), daemon=True).start()
        self.detect()
        self._poll()
        if not self.admin:
            root.after(ELEVATE_DELAY_MS, self.request_admin)

    # ---------- 界面布局 ----------

    def _build(self) -> None:
        root = self.root
        root.title(TITLE)
        root.option_add("*Font", FONT)
        style = ttk.Style(root)
        style.configure(".", font=FONT)
        style.configure("Status.TLabel", font=(FONT[0], 11, "bold"))
        try:
            root.iconphoto(True, tk.PhotoImage(file=str(vision.ASSETS / "projector.png")))
        except tk.TclError:
            pass
        main = ttk.Frame(root, padding=12)
        main.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)

        status = ttk.LabelFrame(main, text="状态", padding=8)
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        self.game_text, self.ocr_text = tk.StringVar(value="检测中……"), tk.StringVar(value="加载中……")
        ttk.Label(status, text="游戏窗口：").grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.game_text).grid(row=0, column=1, sticky="w")
        ttk.Button(status, text="重新检测", command=self.detect).grid(row=0, column=2, sticky="e")
        ttk.Label(status, text="识别模型：").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(status, textvariable=self.ocr_text).grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.admin_text = tk.StringVar(value="有" if self.admin else "没有")
        ttk.Label(status, text="管理员权限：").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(status, textvariable=self.admin_text).grid(row=2, column=1, sticky="w", pady=(4, 0))
        if not self.admin:
            ttk.Button(status, text="获取管理员权限", command=self.request_admin).grid(
                row=2, column=2, sticky="e", pady=(4, 0))

        settings = ttk.LabelFrame(main, text="设置", padding=8)
        settings.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.buys = tk.StringVar(value=str(self.config["buys_per_visit"]))
        self.reserve = tk.StringVar(value=str(self.config["gold_reserve"]))
        self.max_refreshes = tk.StringVar(value=str(self.config["max_refreshes"]))
        self.inputs = []
        for row, (label, var, low, high, hint) in enumerate((
            ("每次进黑市买几次", self.buys, 1, MAX_BUYS_PER_VISIT, f"1～{MAX_BUYS_PER_VISIT}，{MAX_BUYS_PER_VISIT} 次就是 9 个投影仪"),
            ("保底金币", self.reserve, 0, 99999, "买完后至少留这么多金币"),
            ("连续刷新上限", self.max_refreshes, 1, 99999, "刷这么多次还没出现就暂停"),
        )):
            ttk.Label(settings, text=label).grid(row=row, column=0, sticky="w", pady=2)
            box = ttk.Spinbox(settings, from_=low, to=high, textvariable=var, width=8)
            box.grid(row=row, column=1, sticky="w", padx=8, pady=2)
            ttk.Label(settings, text=hint, foreground="#666").grid(row=row, column=2, sticky="w", pady=2)
            self.inputs.append(box)
        keys = f"{self.config['stop_key'].upper()} 停止，{self.config['pause_key'].upper()} 暂停 / 继续"
        ttk.Label(settings, text=f"在游戏里用热键：{keys}；把鼠标甩到屏幕左上角也会停止", foreground="#666").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        stats = ttk.LabelFrame(main, text="统计", padding=8)
        stats.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.stats = tk.StringVar()
        ttk.Label(stats, textvariable=self.stats).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(main)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.start_button = ttk.Button(buttons, text="开始", command=self.start)
        self.pause_button = ttk.Button(buttons, text="暂停", command=self.controls.toggle)
        self.stop_button = ttk.Button(buttons, text="停止", command=self.controls.stopped.set)
        self.diagnose_button = ttk.Button(buttons, text="诊断", command=self.diagnose)
        for column, button in enumerate((self.start_button, self.pause_button, self.stop_button, self.diagnose_button)):
            button.grid(row=0, column=column, padx=(0, 8))

        self.status = tk.StringVar(value="未运行")
        ttk.Label(main, textvariable=self.status, style="Status.TLabel", wraplength=560).grid(
            row=4, column=0, sticky="w", pady=(10, 0))

        logs = ttk.LabelFrame(main, text="日志", padding=6)
        logs.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        main.rowconfigure(5, weight=1)
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(0, weight=1)
        self.log_box = ScrolledText(logs, width=72, height=14, state="disabled", wrap="word", font=(FONT[0], 9))
        self.log_box.grid(sticky="nsew")

    # ---------- 动作 ----------

    def detect(self) -> None:
        try:
            _, description = self.find_game(self.config)
            self.game_text.set(f"已找到（{description}）")
        except SetupError as error:
            self.game_text.set("没找到")
            log(str(error))

    def start(self) -> None:
        if self.running or self.diagnosing:
            return
        try:
            config = self._save_settings()
            if not self.admin:
                raise SetupError("需要管理员权限才能操作游戏：点「获取管理员权限」，在弹出的确认框里选「是」")
            if self.ocr is None:
                raise SetupError("识别模型还没加载好，请稍等")
            hwnd, description = self.find_game(config)
        except SetupError as error:
            self.status.set(str(error))
            log(str(error))
            return
        self.game_text.set(f"已找到（{description}）")
        self.controls.reset()
        self.farm = Farm(self.make_game(hwnd, self.ocr), self.controls, config, sleep=self.sleep,
                         debug_dir=self.debug_dir)
        self.started_at, self.ended_at = time.monotonic(), None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        log("已开始：请切回游戏，游戏在前台时才会操作")

    def request_admin(self) -> None:
        """以管理员身份重新打开界面；新窗口出现后关闭当前窗口，新进程启动失败则留在这里提示。"""
        if self.admin or self.elevated is not None or self.running:
            return
        log("正在申请管理员权限：游戏以管理员身份运行，没有管理员权限的点击会被 Windows 拦截")
        self.admin_text.set("申请中……（确认框没弹出来的话，看看任务栏有没有闪烁的图标）")
        self.root.update_idletasks()
        process = self.relaunch(console=False, owner=int(self.root.wm_frame(), 16))
        if process is None:
            self.admin_text.set("没有")
            log("没有获得管理员权限：可以看界面、做诊断，但不能开始。点「获取管理员权限」可以重试")
            return
        self.elevated = process
        self._watch_elevated()

    def _watch_elevated(self) -> None:
        if winio.process_exited(self.elevated):
            self.elevated = None
            self.admin_text.set("没有")
            log(f"管理员权限的新窗口没能启动，原因记在 {LOG_PATH}")
        elif winio.process_has_window(self.elevated, TITLE):
            log("管理员权限的新窗口已打开，关闭当前窗口")
            self.close()
        else:
            self.root.after(300, self._watch_elevated)

    def diagnose(self) -> None:
        if self.running or self.diagnosing or self.ocr is None:
            return
        self.diagnosing = True
        threading.Thread(target=self._diagnose, daemon=True).start()

    def close(self) -> None:
        if self.running:
            if not messagebox.askyesno(TITLE, "正在运行，确定要停止并退出吗？", parent=self.root):
                return
            self.controls.stopped.set()
            self.thread.join(timeout=3)
        self.root.after_cancel(self._poll_job)
        self.controls.close()
        logger.removeHandler(self.handler)
        self.root.destroy()

    # ---------- 后台线程 ----------

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _load_ocr(self, load_ocr) -> None:
        try:
            self.ocr = load_ocr()
        except Exception as error:  # 模型文件损坏等：界面照常可用，只是不能开始
            self.ocr_error = str(error)
            logger.exception("识别模型加载失败")

    def _run(self) -> None:
        jade_farm.run_farm(self.farm)
        self.ended_at = time.monotonic()

    def _diagnose(self) -> None:
        try:
            hwnd, _ = self.find_game(self.config)
            game = self.make_game(hwnd, self.ocr)
            log(f"诊断：请在 {DIAGNOSE_WAIT} 秒内切回游戏……")
            deadline = time.monotonic() + DIAGNOSE_WAIT
            while not game.ready():
                if time.monotonic() > deadline:
                    log("诊断取消：游戏没有切到前台")
                    return
                time.sleep(0.2)
            time.sleep(0.5)  # 等切换动画结束
            frame = game.frame()
            path = save_shot(frame.image)
            log(f"诊断截图已保存到 {path}\n  {report(frame, self.config['target'])}")
        except (SetupError, Stop) as error:  # Stop：诊断途中游戏窗口被关掉
            log(f"诊断失败：{error}")
        except Exception:
            logger.exception("诊断出错")
        finally:
            self.diagnosing = False

    # ---------- 刷新显示 ----------

    def _save_settings(self) -> dict:
        """读取输入框、校验并存回 config.json。"""
        values = {}
        for key, var, name in (("buys_per_visit", self.buys, "每次进黑市买几次"),
                               ("gold_reserve", self.reserve, "保底金币"),
                               ("max_refreshes", self.max_refreshes, "连续刷新上限")):
            try:
                values[key] = int(var.get().strip())
            except ValueError:
                raise SetupError(f"{name}要填整数") from None
        config = jade_farm.validate_config({**self.config, **values})
        jade_farm.save_config(self.config_path, config)
        self.config = config
        return config

    def _poll(self) -> None:
        self._drain_log()
        running = self.running
        farm = self.farm
        if farm is not None and self.started_at is not None:
            elapsed = int((self.ended_at or time.monotonic()) - self.started_at)
            gold = "—" if farm.gold is None else farm.gold
            self.stats.set(f"已买 {farm.total_bought} 次    已卖 {farm.total_sold} 个    "
                           f"当前金币 {gold}    用时 {elapsed // 60:02d}:{elapsed % 60:02d}")
        else:
            self.stats.set("还没开始")
        if self.ocr is not None:
            self.ocr_text.set("已就绪")
        elif self.ocr_error:
            self.ocr_text.set(f"加载失败：{self.ocr_error}")
        if running:
            paused = self.controls.paused.is_set()
            if paused:
                self.status.set(f"已暂停：{farm.pause_reason or '手动暂停'}。点「继续」或在游戏里按 "
                                f"{self.config['pause_key'].upper()} 恢复")
            else:
                self.status.set("等待切回游戏……" if farm.background else "运行中")
            self.pause_button.configure(text="继续" if paused else "暂停")
        elif farm is not None and self.status.get().startswith(("运行中", "已暂停", "等待切回")):
            self.status.set("已停止")
        idle = "disabled" if running or self.diagnosing or self.ocr is None else "normal"
        self.start_button.configure(state=idle)
        self.diagnose_button.configure(state=idle)
        for widget in (self.pause_button, self.stop_button):
            widget.configure(state="normal" if running else "disabled")
        for box in self.inputs:
            box.configure(state="disabled" if running else "normal")
        self._poll_job = self.root.after(POLL_MS, self._poll)

    def _drain_log(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.lines.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", "\n".join(lines) + "\n")
        overflow = int(self.log_box.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if overflow > 0:
            self.log_box.delete("1.0", f"{overflow + 1}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


def _fatal(message: str) -> None:
    """记进日志并弹出系统提示框（不依赖 tkinter，界面本身起不来时也能显示）。"""
    logger.error(message)
    winio.message_box(TITLE, f"{message}\n\n详细信息已记录到 {LOG_PATH}")


def _log_to_file() -> None:
    """pythonw 没有控制台，出了问题只能看日志文件。日志超过 2MB 自动轮换，最多保留 3 份。"""
    LOG_PATH.parent.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)


def main() -> None:
    _log_to_file()
    sys.excepthook = lambda *exc: _fatal("程序出错：\n" + "".join(traceback.format_exception(*exc)))
    threading.excepthook = lambda args: logger.error("后台线程出错：\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    winio.enable_dpi_awareness()  # 必须在创建窗口之前，否则截图坐标和界面缩放会对不上
    log(f"启动界面：{sys.executable}，管理员权限：{'有' if winio.is_admin() else '没有'}")
    root = None
    try:
        root = tk.Tk()
        App(root)
    except Exception:
        if root is not None:
            root.destroy()
        _fatal("界面启动失败：\n" + traceback.format_exc())
        return
    root.mainloop()


if __name__ == "__main__":
    main()
