"""诊断工具：在游戏里按 F8 截图并打印识别结果，按停止键（默认 F10）退出。

截图按原始分辨率保存到 shots/，可以用来排查识别问题，也可以补充进识别测试。
第一次使用 jade_farm.py 之前，建议先用它确认：能找到游戏窗口、截图不是黑屏、黑市能被正确识别。
"""
import threading
from datetime import datetime
from pathlib import Path

import keyboard

import vision
from jade_farm import CONFIG_PATH, ROOT, SetupError, connect, enable_console_log, load_config, show

SHOT_KEY = "f8"
SHOTS_DIR = ROOT / "shots"


def save_shot(image) -> Path:
    SHOTS_DIR.mkdir(exist_ok=True)
    path = SHOTS_DIR / f"capture-{datetime.now():%Y%m%d-%H%M%S}.png"
    image.save(path)
    return path


def report(frame: vision.Frame, target: str) -> str:
    if frame.rgb.mean() < 5:
        return "截图几乎全黑：游戏可能是独占全屏，请在游戏设置里改成窗口化或无边框"
    lines = [f"画面：{frame.page}，金币：{show(frame.gold)}"]
    if frame.page == vision.MARKET:
        cards = " / ".join(f"{card.name or '?'}({show(frame.price(card))})" for card in frame.cards)
        lines.append(f"轮换商品：{cards}")
        found = next((c for c in frame.cards if vision.similar(c.name, target)), None)
        lines.append(f"目标「{target}」：" + (f"第 {found.index + 1} 格，点击位置 {found.buy_point}" if found else "没有"))
    elif frame.page == vision.BOARD:
        lines.append(f"黑市入口：{show(frame.market_button())}")
        lines.append(f"完美投影仪：{show(frame.projector)}，剩余 {show(frame.projector_count())} 个")
        lines.append(f"翡翠：{show(frame.jade)}")
        occupied = [i for i in range(len(vision.BENCH_POINTS)) if frame.bench_occupied(i)]
        lines.append("备战席有角色的格子：" + ("、".join(
            f"{i + 1}{'（翡翠）' if frame.jade_on_bench(i) else ''}" for i in occupied) or "无"))
    return "\n  ".join(lines)


def main() -> None:
    enable_console_log()
    try:
        config = load_config(CONFIG_PATH)
        game = connect(config)
    except SetupError as error:
        raise SystemExit(str(error))
    if game is None:
        return
    shot, quit_ = threading.Event(), threading.Event()
    keyboard.add_hotkey(SHOT_KEY, shot.set)
    keyboard.add_hotkey(config["stop_key"], quit_.set)
    print(f"在游戏里按 {SHOT_KEY.upper()} 截图并识别，按 {config['stop_key'].upper()} 退出")
    while not quit_.is_set():
        if not shot.wait(0.2):
            continue
        shot.clear()
        image = game.screenshot()
        path = save_shot(image)
        print(f"已保存 {path}（{image.width}×{image.height}）\n  {report(vision.Frame(image, game.ocr), config['target'])}")


if __name__ == "__main__":
    main()
