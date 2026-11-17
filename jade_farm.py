"""货币战争 · 三星翡翠自动刷钱：黑市买完美投影仪 → 拖到翡翠身上复制 → 复制品拖进出售区卖掉，循环。

命令行用法：三星翡翠在场的备战阶段，运行  python jade_farm.py（图形界面见 gui.py）
F10 停止，F11 暂停 / 继续；鼠标甩到屏幕左上角也会停止。
"""
import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import keyboard

import vision
import winio
from vision import BOARD, DETAIL, MARKET, Frame

def _data_dir() -> Path:
    """配置、日志和截图存放的目录。打包成 exe 后放在 exe 旁边，因为单文件 exe 每次运行的解压目录都会变。"""
    compiled = globals().get("__compiled__")  # Nuitka 编译后才有
    if compiled is not None:
        return Path(getattr(compiled, "containing_dir", None) or os.path.dirname(os.path.abspath(sys.argv[0])))
    return Path(__file__).resolve().parent


ROOT = _data_dir()
CONFIG_PATH = ROOT / "config.json"
DEBUG_DIR = ROOT / "debug"
MAX_BUYS_PER_VISIT = 3   # 每次进黑市最多买 3 次（9 个投影仪）
DEFAULTS = {
    "window_titles": ["崩坏：星穹铁道", "Honkai: Star Rail"],
    "target": "完美投影仪",
    "buys_per_visit": 1,
    "gold_reserve": 0,
    "max_refreshes": 300,
    "stop_key": "f10",
    "pause_key": "f11",
}
CONFIG_HINTS = {
    "window_titles": "游戏窗口标题应为非空的文字列表",
    "target": "要买的商品名不能为空",
    "buys_per_visit": f"每次进黑市买几次应为 1～{MAX_BUYS_PER_VISIT}",
    "gold_reserve": "保底金币应为 0 或正整数",
    "max_refreshes": "连续刷新上限应为正整数",
    "stop_key": "停止键不能为空",
    "pause_key": "暂停键不能为空",
}

POLL = 0.25             # 等待画面变化时的截图间隔（秒）
REFRESH_TIMEOUT = 3.0   # 点刷新后等商品变化
BUY_TIMEOUT = 3.0       # 点商品后等购买到账
OPEN_TIMEOUT = 4.0      # 点黑市入口后等界面出现
CLOSE_TIMEOUT = 3.0     # 点空白处后等黑市或角色详情关闭
COPY_TIMEOUT = 3.0      # 投影仪拖到翡翠身上后等复制品出现
SELL_TIMEOUT = 3.0      # 复制品拖进出售区后等它消失
SETTLE_TIMEOUT = 2.0    # 等动画结束
CHANGE_DIFF = 6.0       # 商品区域平均像素差超过它，视为已刷新
STABLE_DIFF = 2.0       # 相邻两帧差低于它，视为动画结束
UNKNOWN_LIMIT = 6       # 连续这么多帧认不出画面就暂停（约 3 秒）

logger = logging.getLogger("jade_farm")
logger.setLevel(logging.INFO)
LOG_FORMAT = logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S")


class Stop(Exception):
    """用户要求停止，或游戏窗口已关闭。"""


class SetupError(Exception):
    """运行前的检查没通过（配置有误、没找到游戏窗口等），消息直接给用户看。"""


def log(message: str) -> None:
    logger.info(message)


def enable_console_log() -> None:
    """命令行版：日志打印到控制台。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LOG_FORMAT)
    logger.addHandler(handler)


def show(value) -> str:
    return "未识别" if value is None else str(value)


def validate_config(data: dict) -> dict:
    """补全默认值并校验取值，不合法时抛出 SetupError。"""
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise SetupError(f"配置里有不认识的项：{', '.join(unknown)}")
    config = {**DEFAULTS, **data}

    def count(minimum, maximum=None):
        return lambda v: type(v) is int and v >= minimum and (maximum is None or v <= maximum)

    def text(v):
        return isinstance(v, str) and bool(v.strip())

    checks = {
        "window_titles": lambda v: isinstance(v, list) and bool(v) and all(map(text, v)),
        "target": text,
        "buys_per_visit": count(1, MAX_BUYS_PER_VISIT),
        "gold_reserve": count(0),
        "max_refreshes": count(1),
        "stop_key": text,
        "pause_key": text,
    }
    bad = [key for key, valid in checks.items() if not valid(config[key])]
    if bad:
        raise SetupError("配置有误：" + "；".join(CONFIG_HINTS[key] for key in bad))
    return config


def load_config(path: Path) -> dict:
    """读取 config.json（文件不存在或缺项时用默认值），并校验取值。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SetupError(f"配置文件 {path} 格式错误：{error}")
    if not isinstance(data, dict):
        raise SetupError(f"配置文件 {path} 的内容应该是一个 JSON 对象")
    return validate_config(data)


def save_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(validate_config(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Controls:
    """停止 / 暂停信号。可绑定全局热键；鼠标甩到屏幕左上角也算停止（热键失灵时的兜底）。"""

    def __init__(self, stop_key: str | None = None, pause_key: str | None = None):
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self._hotkeys = [keyboard.add_hotkey(key, action)
                         for key, action in ((stop_key, self.stopped.set), (pause_key, self.toggle)) if key]

    def toggle(self) -> None:
        if self.paused.is_set():
            self.paused.clear()
        else:
            self.paused.set()

    def reset(self) -> None:
        self.stopped.clear()
        self.paused.clear()

    def close(self) -> None:
        for hotkey in self._hotkeys:
            keyboard.remove_hotkey(hotkey)
        self._hotkeys = []

    def stop_requested(self) -> bool:
        x, y = winio.cursor_pos()
        return self.stopped.is_set() or (x <= 2 and y <= 2)

    def alert(self) -> None:
        winio.beep()


class Game:
    """游戏窗口：截图，以及把设计坐标换算成屏幕坐标后点击、拖动。"""

    def __init__(self, hwnd: int, ocr: vision.Ocr):
        self.hwnd = hwnd
        self.ocr = ocr

    def screenshot(self):
        return winio.grab(self._rect())

    def frame(self) -> Frame:
        return Frame(self.screenshot(), self.ocr)

    def click(self, point: tuple[int, int]) -> None:
        rect = self._rect()
        winio.click(*vision.to_screen(point, rect))
        self._rest(rect)

    def drag(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        rect = self._rect()
        winio.drag(vision.to_screen(start, rect), vision.to_screen(end, rect))
        self._rest(rect)

    def ready(self) -> bool:
        self._rect()
        return winio.is_foreground(self.hwnd)

    def _rect(self) -> tuple[int, int, int, int]:
        if not winio.window_exists(self.hwnd):
            raise Stop("游戏窗口已关闭")
        return winio.client_rect(self.hwnd)

    @staticmethod
    def _rest(rect) -> None:
        winio.move(*vision.to_screen(vision.REST_POINT, rect))


def ensure_admin() -> bool:
    """命令行版：有管理员权限时返回 True；否则在新的管理员控制台里重新运行并返回 False（当前进程应退出）。"""
    if winio.is_admin():
        return True
    log("需要管理员权限：游戏以管理员身份运行，普通权限发出的点击会被 Windows 拦截。正在请求提权……")
    if winio.relaunch_as_admin(console=True) is not None:
        return False
    raise SetupError("没有获得管理员权限，已退出")


def find_game(config: dict) -> tuple[int, str]:
    """找到游戏窗口并检查分辨率，返回 (窗口句柄, 描述)。找不到或不是 16:9 时抛出 SetupError。"""
    hwnd = winio.find_window(config["window_titles"])
    if hwnd is None:
        others = winio.unity_window_titles()
        hint = f"。当前的 Unity 窗口：{'、'.join(others)}，可把正确标题填进 config.json 的 window_titles" if others else ""
        raise SetupError(f"没找到游戏窗口（标题：{'、'.join(config['window_titles'])}），请先进入游戏{hint}")
    _, _, width, height = winio.client_rect(hwnd)
    if height == 0 or abs(width / height - 16 / 9) > 0.01:
        raise SetupError(f"游戏画面是 {width}×{height}，需要 16:9 的分辨率（如 1920×1080、2560×1440）")
    return hwnd, f"{winio.window_title(hwnd)}，{width}×{height}"


def connect(config: dict) -> Game | None:
    """命令行版的运行准备：DPI、管理员权限、游戏窗口、OCR 模型。返回 None 表示已在管理员窗口里重新启动。"""
    winio.enable_dpi_awareness()
    if not ensure_admin():
        return None
    hwnd, description = find_game(config)
    log(f"找到游戏窗口：{description}")
    log("加载 OCR 模型……")
    return Game(hwnd, vision.Ocr())


class Farm:
    """状态机：
    黑市：本次还没买够就找目标，有就买、没有就刷新；买够了就关掉黑市。
    棋盘：先卖掉刚复制出来的翡翠；还有投影仪就拖到翡翠身上；没有了就打开黑市。
    角色详情：关掉。认不出画面：暂停。
    """

    def __init__(self, game, controls, config: dict, *, sleep=time.sleep, debug_dir: Path | None = None):
        self.game = game
        self.controls = controls
        self.cfg = config
        self.sleep = sleep
        self.debug_dir = debug_dir
        self.visit_bought = 0     # 本次进黑市已经买了几次
        self.total_bought = 0
        self.total_sold = 0
        self.gold = None          # 最近一次读到的金币，供界面显示
        self.pause_reason = None  # 程序自己暂停时的原因，供界面显示
        self.refreshes = 0        # 自上次买到目标以来的刷新次数
        self.pending = None       # 上次用投影仪前空着的备战席格子，复制品会出现在其中
        self.unknown = 0
        self.background = False

    def run(self) -> None:
        while True:
            self.checkpoint()
            frame = self.game.frame()
            page = frame.page
            if page in (MARKET, BOARD) and frame.gold is not None:
                self.gold = frame.gold
            self.unknown = self.unknown + 1 if page not in (MARKET, BOARD, DETAIL) else 0
            if page == MARKET:
                self.on_market(frame)
            elif page == BOARD:
                self.on_board(frame)
            elif page == DETAIL:
                self.close(frame, "角色详情")
            elif self.unknown >= UNKNOWN_LIMIT:
                self.unknown = 0
                self.pause("认不出当前画面（不是备战棋盘、黑市或角色详情）", frame)
            else:
                self.sleep(0.5)

    def checkpoint(self) -> None:
        """每次操作前调用：处理停止、暂停，以及游戏不在前台。"""
        while True:
            if self.controls.stop_requested():
                raise Stop
            if self.controls.paused.is_set():
                self.sleep(0.2)
                continue
            self.pause_reason = None
            if not self.game.ready():
                if not self.background:
                    self.background = True
                    log("游戏不在前台，等待切回……")
                self.sleep(0.5)
            elif self.background:
                self.background = False
                log("游戏已回到前台，1 秒后继续")
                self.sleep(1.0)
            else:
                return

    # ---------- 黑市 ----------

    def on_market(self, frame: Frame) -> None:
        if self.visit_bought >= self.cfg["buys_per_visit"]:
            self.close(frame, "黑市")
            return
        target = self.cfg["target"]
        names = " / ".join(card.name or "?" for card in frame.cards)
        card = next((c for c in frame.cards if vision.similar(c.name, target)), None)
        if card is None:
            if self.refreshes >= self.cfg["max_refreshes"]:
                self.refreshes = 0
                self.pause(f"已连续刷新 {self.cfg['max_refreshes']} 次仍未出现{target}", frame)
                return
            self.refreshes += 1
            log(f"黑市：{names} → 第 {self.refreshes} 次刷新")
            self.refresh(frame)
            return
        price, gold = frame.price(card), frame.gold
        log(f"黑市：{names} → 发现{target}（第 {card.index + 1} 格，价格 {show(price)}，当前金币 {show(gold)}）")
        reserve = self.cfg["gold_reserve"]
        if gold is not None and price is not None and gold - price < reserve:
            self.pause(f"金币不够：买完只剩 {gold - price}，低于保底 {reserve}", frame)
            return
        self.buy(frame, card)

    def refresh(self, before: Frame) -> None:
        for _ in range(2):  # 第一次没反应再点一次
            self.game.click(vision.MARKET_REFRESH)
            changed = self.wait_for(
                lambda f: f.page != MARKET or f.diff(before, vision.CARDS_AREA) > CHANGE_DIFF, REFRESH_TIMEOUT)
            if changed is not None:
                self.settle(changed)
                return
        self.pause("点了刷新但商品没变（可能刷新要花钱而金币不够，或点击没被游戏接收）", before)

    def buy(self, frame: Frame, card: vision.Card) -> None:
        target, gold = self.cfg["target"], frame.gold

        def bought(f: Frame) -> bool:
            if gold is not None and f.gold is not None and f.gold < gold:
                return True
            return f.page == MARKET and not vision.similar(f.cards[card.index].name, target)

        self.game.click(card.buy_point)
        result = self.wait_for(bought, BUY_TIMEOUT)
        if result is None:
            # 点商品后可能弹出确认框：只在这里找，并且只点一次
            confirm = self.game.frame().confirm_button()
            if confirm is not None:
                log("出现确认框，点击确认")
                self.game.click(confirm)
                result = self.wait_for(bought, BUY_TIMEOUT)
        if result is not None:
            result = self.settle(result)  # 动画中途的误读在画面稳定后复核
        if result is None or not bought(result):
            self.pause(f"点了{target}，但没确认到购买成功", self.game.frame())
            return
        self.visit_bought += 1
        self.total_bought += 1
        self.refreshes = 0
        self.gold = result.gold if result.gold is not None else self.gold
        log(f"购买成功：金币 {show(gold)} → {show(result.gold)}"
            f"（这次进黑市第 {self.visit_bought}/{self.cfg['buys_per_visit']} 次，累计 {self.total_bought} 次）")

    def open_market(self, frame: Frame) -> None:
        point = frame.market_button()
        if point is None:
            self.pause("棋盘上没找到「黑市主理人」入口（三星翡翠在场吗？）", frame)
            return
        log("没有投影仪了，打开黑市")
        self.visit_bought = 0
        self.game.click(point)
        opened = self.wait_for(lambda f: f.page == MARKET, OPEN_TIMEOUT)
        if opened is None:
            self.pause("点了黑市入口，但黑市没有打开", self.game.frame())
        else:
            self.settle(opened)  # 等打开动画结束再识别商品

    def close(self, frame: Frame, name: str) -> None:
        """点左上角空白处关闭黑市或角色详情。"""
        for _ in range(2):
            self.game.click(vision.CLOSE_POINT)
            closed = self.wait_for(lambda f: f.page == BOARD, CLOSE_TIMEOUT)
            if closed is not None:
                log(f"已关闭{name}")
                self.settle(closed, vision.BENCH_AREA)
                return
        self.pause(f"点左上角空白处没能关掉{name}", self.game.frame())

    # ---------- 棋盘 ----------

    def on_board(self, frame: Frame) -> None:
        if self.pending:
            copies = [index for index in self.pending if frame.bench_occupied(index)]
            self.pending = None
            if copies:
                self.sell_copies(frame, copies)
                return
        if frame.projector is None or frame.projector_count() == 0:
            self.open_market(frame)
        else:
            self.use_projector(frame)

    def use_projector(self, frame: Frame) -> None:
        jade = frame.jade
        if jade is None:
            self.pause("前台和后台都没找到翡翠，没法用投影仪", frame)
            return
        empty = frame.empty_bench()
        if not empty:
            self.pause("备战席满了，复制品没地方放", frame)
            return
        log(f"把完美投影仪拖到翡翠身上（剩余 {show(frame.projector_count())} 个）")
        self.pending = empty
        for _ in range(2):  # 第一次没反应再拖一次
            self.game.drag(frame.projector, jade)
            copied = self.wait_for(
                lambda f: f.page == BOARD and any(f.bench_occupied(index) for index in empty), COPY_TIMEOUT)
            if copied is not None:
                self.settle(copied, vision.BENCH_AREA)  # 等复制品出现的动画结束，下一轮再卖
                return
        self.pause("投影仪拖到翡翠身上后，备战席没有出现复制品", self.game.frame())

    def sell_copies(self, frame: Frame, slots: list[int]) -> None:
        for index in slots:
            if not frame.jade_on_bench(index):
                self.pause(f"备战席第 {index + 1} 格新出现的角色不像翡翠，没有卖", frame)
                return
            frame = self.sell(frame, index)
            if frame is None:
                return

    def sell(self, frame: Frame, index: int) -> Frame | None:
        gold = frame.gold
        self.game.drag(vision.BENCH_POINTS[index], vision.SELL_POINT)
        sold = self.wait_for(lambda f: f.page == BOARD and not f.bench_occupied(index), SELL_TIMEOUT)
        if sold is not None:
            sold = self.settle(sold, vision.BENCH_AREA)  # 没卖掉的卡飞回原位时会短暂"消失"，稳定后再确认
        if sold is None or sold.bench_occupied(index):
            self.pause(f"把备战席第 {index + 1} 格的复制品拖进出售区后，它还在", self.game.frame())
            return None
        self.total_sold += 1
        self.gold = sold.gold if sold.gold is not None else self.gold
        log(f"卖出复制品：金币 {show(gold)} → {show(sold.gold)}（累计卖出 {self.total_sold} 个）")
        return sold

    # ---------- 通用 ----------

    def wait_for(self, condition, timeout: float) -> Frame | None:
        for _ in range(round(timeout / POLL)):
            self.sleep(POLL)
            if self.controls.stop_requested():
                raise Stop
            frame = self.game.frame()
            if condition(frame):
                return frame
        return None

    def settle(self, frame: Frame, area=vision.CARDS_AREA) -> Frame:
        """等动画结束（相邻两帧在该区域几乎不变）。超时也返回最新一帧，由后续识别兜底。"""
        for _ in range(round(SETTLE_TIMEOUT / POLL)):
            self.sleep(POLL)
            current = self.game.frame()
            if current.diff(frame, area) < STABLE_DIFF:
                return current
            frame = current
        return frame

    def pause(self, reason: str, frame: Frame | None = None) -> None:
        self.controls.paused.set()
        self.pause_reason = reason
        self.pending = None  # 暂停期间画面可能被手动改动，恢复后不再按旧记录卖东西
        saved = self.save_debug(frame) if frame is not None else None
        log(f"已暂停：{reason}" + (f"（当时的画面已保存到 {saved}）" if saved else ""))
        log(f"处理好后按 {self.cfg['pause_key'].upper()} 继续，按 {self.cfg['stop_key'].upper()} 退出")
        self.controls.alert()

    def save_debug(self, frame: Frame) -> Path | None:
        if self.debug_dir is None:
            return None
        self.debug_dir.mkdir(exist_ok=True)
        path = self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        frame.image.save(path)
        return path


def run_farm(farm: Farm) -> bool:
    """运行到停止为止，最后记录结束原因和统计。正常停止返回 True，出错返回 False。"""
    ok = True
    try:
        farm.run()
    except (Stop, KeyboardInterrupt) as stop:
        log(f"已停止{f'：{stop}' if str(stop) else ''}")
    except Exception:
        ok = False
        logger.exception("运行出错，已停止。请把错误信息和 debug 文件夹里的截图发给开发者")
    log(f"本次共买 {farm.total_bought} 次投影仪，卖出 {farm.total_sold} 个复制品")
    return ok


def main() -> None:
    enable_console_log()
    parser = argparse.ArgumentParser(description="货币战争三星翡翠：黑市买完美投影仪，复制翡翠卖钱")
    parser.add_argument("--config", type=Path, help="配置文件路径（默认用脚本目录下的 config.json）")
    path = parser.parse_args().config
    try:
        if path is not None and not path.exists():
            raise SetupError(f"找不到配置文件 {path}")
        config = load_config(path or CONFIG_PATH)
        game = connect(config)
    except SetupError as error:
        raise SystemExit(str(error))
    if game is None:
        return
    controls = Controls(config["stop_key"], config["pause_key"])
    farm = Farm(game, controls, config, debug_dir=DEBUG_DIR)
    log(f"3 秒后开始，请切回游戏。{config['stop_key'].upper()} 停止，{config['pause_key'].upper()} 暂停/继续，"
        "鼠标甩到屏幕左上角也会停止")
    time.sleep(3)
    if not run_farm(farm):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
