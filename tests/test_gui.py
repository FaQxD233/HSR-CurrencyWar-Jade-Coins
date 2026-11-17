"""界面测试：隐藏窗口里用截图模拟的游戏跑一整轮，核对设置保存、日志、统计和按钮状态。不需要打开游戏。"""
import time
import tkinter as tk

import pytest

import gui
import jade_farm
from fakes import LOOP, FakeGame
from jade_farm import Controls


def pump(root, condition, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def make_app(tmp_path, ocr):
    apps = []

    def make(game=None, *, admin=True, relaunch=None):
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root, config_path=tmp_path / "config.json",
                      find_game=lambda config: (1, "崩坏：星穹铁道，2560×1440"),
                      make_game=lambda hwnd, ocr_: game, load_ocr=lambda: ocr,
                      controls=Controls(), debug_dir=None, sleep=lambda _: None,
                      is_admin=lambda: admin, relaunch=relaunch)
        apps.append(app)
        assert pump(root, lambda: app.ocr is not None)
        return app

    yield make
    for app in apps:
        app.close()


def test_start_runs_full_cycle_and_saves_settings(make_app, frames, tmp_path):
    app = make_app(FakeGame(frames, "board_done", LOOP, limit=7))
    app.reserve.set("5")
    app.start()
    assert pump(app.root, lambda: app.thread is not None and not app.thread.is_alive())
    app.root.update()
    app._poll()  # 再刷新一次显示，取出线程结束前的最后几行日志
    text = app.log_box.get("1.0", "end")
    assert "购买成功" in text and "卖出复制品" in text and "本次共买 1 次投影仪，卖出 1 个复制品" in text
    assert app.stats.get().startswith("已买 1 次    已卖 1 个")
    assert app.status.get() == "已停止"
    assert str(app.start_button["state"]) == "normal" and str(app.stop_button["state"]) == "disabled"
    assert jade_farm.load_config(tmp_path / "config.json")["gold_reserve"] == 5


def test_without_admin_requests_elevation_from_the_window_and_refuses_to_start(make_app):
    calls = []

    def declined(**kwargs):  # 模拟用户在 UAC 确认框里点了「否」
        calls.append(kwargs)
        return None

    app = make_app(admin=False, relaunch=declined)
    assert pump(app.root, lambda: calls and app.admin_text.get() == "没有")
    assert calls[0]["console"] is False and calls[0]["owner"]  # 由界面窗口发起，确认框才会在最前面
    app.start()
    assert app.thread is None
    assert "管理员权限" in app.status.get()


def test_invalid_settings_do_not_start(make_app, tmp_path):
    app = make_app()
    for value, hint in (("4", "1～3"), ("abc", "要填整数")):
        app.buys.set(value)
        app.start()
        assert app.thread is None
        assert hint in app.status.get()
    assert not (tmp_path / "config.json").exists()
