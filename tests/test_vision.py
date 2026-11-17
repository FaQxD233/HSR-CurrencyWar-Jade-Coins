"""识别单测：在实机截图上核对页面判定、商品名、价格、金币、黑市入口、投影仪、翡翠和备战席。"""
import pytest

import jade_farm
import vision


def near(point, expected, tolerance=6):
    return point is not None and abs(point[0] - expected[0]) <= tolerance and abs(point[1] - expected[1]) <= tolerance


def test_page(frames):
    for key in ("board_first", "board", "drag"):
        assert frames[key].page == vision.BOARD, key
    assert frames["market"].page == vision.MARKET
    assert frames["target"].page == vision.MARKET
    assert frames["detail"].page == vision.DETAIL


def test_card_names(frames):
    assert [card.name for card in frames["market"].cards] == ["8经验", "幸运星", "物质分解液"]
    assert [card.name for card in frames["target"].cards] == ["和平手枪", "星徽秘典", "完美投影仪"]


@pytest.mark.parametrize(("key", "prices"), [("market", [4, 6, 12]), ("target", [6, 15, 9])])
def test_prices(frames, key, prices):
    frame = frames[key]
    assert [frame.price(card) for card in frame.cards] == prices


def test_gold(frames):
    assert [frames[key].gold for key in ("board_first", "market", "target")] == [23, 23, 23]
    assert [frames[key].gold for key in ("board", "drag")] == [340, 340]
    assert frames["detail"].gold is None  # 详情面板盖住金币栏，那里的 14 是出售价


def test_market_button_follows_bond_order(frames):
    assert near(frames["board_first"].market_button(), (245, 347))  # 「黑市主理人」在羁绊列表第 2 行
    assert near(frames["board"].market_button(), (246, 429))        # 换到第 3 行后也能找到


def test_projector(frames):
    assert near(frames["board_first"].projector, (1685, 156), 3)
    assert frames["board_first"].projector_count() == 9
    assert near(frames["board"].projector, (1685, 156), 3)
    assert frames["board"].projector_count() == 3
    assert frames["board_done"].projector is None
    assert frames["board_done"].projector_count() == 0


def test_jade_on_board(frames):
    for key in ("board_first", "board", "drag", "detail"):
        assert near(frames[key].jade, (1170, 407), 3), key
    assert frames["market"].jade is None  # 黑市挡住了棋盘


def test_bench(frames):
    first = frames["board_first"]
    assert first.empty_bench() == [0, 3, 4, 5, 6, 7, 8]
    assert not first.jade_on_bench(1) and not first.jade_on_bench(2)  # 别的角色不会被当成翡翠
    board = frames["board"]
    assert board.empty_bench() == [1, 2, 3, 4, 5, 6, 7, 8]
    assert board.jade_on_bench(0)
    assert frames["board_ready"].empty_bench() == list(range(9))
    assert frames["bench_full"].empty_bench() == []


def test_no_confirm_dialog_on_plain_market(frames):
    assert frames["target"].confirm_button() is None


def test_refresh_changes_card_area(frames):
    assert frames["market"].diff(frames["target"], vision.CARDS_AREA) > jade_farm.CHANGE_DIFF
    assert frames["market"].diff(frames["market"], vision.CARDS_AREA) == 0


def test_similar():
    assert vision.similar("完美投影仪", "完美投影仪")
    assert vision.similar("完美投影议", "完美投影仪")       # OCR 错一个字
    assert not vision.similar("员工投影仪", "完美投影仪")   # 普通投影仪不能复制五费，绝不能买错
    assert not vision.similar("", "完美投影仪")


def test_to_screen():
    assert vision.to_screen(vision.MARKET_REFRESH, (0, 0, 2560, 1440)) == (1215, 683)
    assert vision.to_screen((0, 0), (100, 50, 1920, 1080)) == (100, 50)
