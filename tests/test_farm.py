"""状态机测试：用实机截图模拟游戏，按点击 / 拖动的位置切换画面，核对操作顺序和暂停条件。"""
import pytest
from PIL import ImageOps

import jade_farm
import vision
from fakes import LOOP, FakeControls, FakeGame
from jade_farm import Farm, SetupError, Stop


def run(frames, start, transitions, *, limit=None, paused=True, **config):
    game, controls = FakeGame(frames, start, transitions, limit), FakeControls()
    farm = Farm(game, controls, {**jade_farm.DEFAULTS, **config}, sleep=lambda _: None)
    with pytest.raises(Stop):
        farm.run()
    assert controls.paused.is_set() == paused
    return farm, game


def test_full_cycle_buy_copy_sell(frames):
    farm, game = run(frames, "board_done", LOOP, limit=7, paused=False)
    assert game.actions == ["market_button", "refresh", "card2", "close",
                            "projector->jade", "bench0->sell", "market_button"]
    assert (farm.total_bought, farm.total_sold) == (1, 1)
    assert farm.gold == 23  # 记录的是最后读到的金币：最后一帧是黑市截图（金币 23）


def test_buys_up_to_quota_then_closes_market(frames):
    farm, game = run(frames, "target", LOOP, limit=4, paused=False, buys_per_visit=2)
    assert game.actions == ["card2", "refresh", "card2", "close"]
    assert farm.total_bought == 2


def test_detail_panel_is_closed(frames):
    _, game = run(frames, "detail", {("detail", "close"): "board_done"}, limit=1, paused=False)
    assert game.actions == ["close"]


def test_gold_reserve_blocks_purchase(frames):
    farm, game = run(frames, "target", {}, gold_reserve=20)  # 23 - 9 = 14，低于保底 20
    assert game.actions == []
    assert farm.total_bought == 0
    assert "保底" in farm.pause_reason


def test_pauses_when_refresh_has_no_effect(frames):
    _, game = run(frames, "market", {})
    assert game.actions == ["refresh", "refresh"]


def test_pauses_when_purchase_not_confirmed(frames):
    farm, game = run(frames, "target", {})
    assert game.actions == ["card2"]
    assert farm.total_bought == 0


def test_pauses_after_max_refreshes(frames, ocr):
    image = frames["market"].image.copy()
    icons = (540, 175, 1260, 370)  # 只改商品图标，名字不变，但像素明显变化
    image.paste(ImageOps.invert(image.crop(icons)), icons[:2])
    frames = {**frames, "market_b": vision.Frame(image, ocr)}
    _, game = run(frames, "market", {("market", "refresh"): "market_b", ("market_b", "refresh"): "market"},
                  max_refreshes=3)
    assert game.actions == ["refresh"] * 3


def test_pauses_when_copy_does_not_appear(frames):
    _, game = run(frames, "board_ready", {})
    assert game.actions == ["projector->jade"] * 2


def test_never_sells_a_new_unit_that_is_not_jade(frames):
    # 用投影仪后备战席第 2、3 格出现的是别的角色：暂停，不卖
    farm, game = run(frames, "board_ready", {("board_ready", "projector->jade"): "board_first"})
    assert game.actions == ["projector->jade"]
    assert farm.total_sold == 0


def test_pauses_when_sale_fails(frames):
    farm, game = run(frames, "board_ready", {("board_ready", "projector->jade"): "board"})
    assert game.actions == ["projector->jade", "bench0->sell"]
    assert farm.total_sold == 0


def test_bench_full_pauses_before_using_projector(frames):
    _, game = run(frames, "bench_full", {})
    assert game.actions == []


def test_load_and_save_config(tmp_path):
    path = tmp_path / "config.json"
    assert jade_farm.load_config(path) == jade_farm.DEFAULTS  # 文件不存在时用默认值
    jade_farm.save_config(path, {**jade_farm.DEFAULTS, "buys_per_visit": 3, "gold_reserve": 5})
    assert jade_farm.load_config(path)["buys_per_visit"] == 3
    for bad in ('{"buys_per_visit": 0}', '{"buys_per_visit": 4}', '{"gold_reserve": "5"}',
                '{"buys_per_round": 1}', '[1]', '{oops'):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(SetupError):
            jade_farm.load_config(path)
    with pytest.raises(SetupError):  # 不合法的配置不会被写进文件
        jade_farm.save_config(path, {**jade_farm.DEFAULTS, "gold_reserve": -1})
