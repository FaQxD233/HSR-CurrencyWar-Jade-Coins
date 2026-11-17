"""测试共用夹具：用 shots/ 里的实机截图（2560×1440）构造画面，并派生出几种棋盘状态。"""
from pathlib import Path

import pytest
from PIL import Image

import vision

SHOTS = Path(__file__).resolve().parents[1] / "shots"
SCREENS = {
    "board_first": "屏幕截图 2026-11-17 220621.png",  # 棋盘：金币 23，投影仪 9 个，备战席 2、3 格是别的角色
    "market": "屏幕截图 2026-11-17 220632.png",       # 黑市：8经验 / 幸运星 / 物质分解液
    "target": "屏幕截图 2026-11-17 220649.png",       # 黑市：和平手枪 / 星徽秘典 / 完美投影仪
    "detail": "屏幕截图 2026-11-17 225045.png",       # 备战席翡翠复制品的角色详情（出售 +14）
    "drag": "屏幕截图 2026-11-17 225057.png",         # 正在拖动复制品，左下角出现出售区
    "board": "屏幕截图 2026-11-17 225129.png",        # 棋盘：金币 340，投影仪 3 个，复制品在备战席第 1 格
}


def _slot_box(index):
    x, _ = vision.BENCH_POINTS[index]
    return x - 62, 850, x + 63, 978


def _copy_slot(image, source, targets):
    patch = image.crop(_slot_box(source))
    for index in targets:
        image.paste(patch, _slot_box(index)[:2])
    return image


@pytest.fixture(scope="session")
def ocr():
    return vision.Ocr()


@pytest.fixture(scope="session")
def frames(ocr):
    missing = [name for name in SCREENS.values() if not (SHOTS / name).exists()]
    if missing:
        pytest.skip(f"shots/ 里缺少截图：{missing}")
    images = {key: Image.open(SHOTS / name).convert("RGB").resize(vision.DESIGN_SIZE, Image.LANCZOS)
              for key, name in SCREENS.items()}
    # 派生状态：备战席清空（还剩投影仪）、投影仪也用完、备战席 9 格全是翡翠复制品
    images["board_ready"] = _copy_slot(images["board"].copy(), 1, [0])
    images["board_done"] = images["board_ready"].copy()
    images["board_done"].paste((0, 0, 0), (1655, 125, 1735, 200))
    images["bench_full"] = _copy_slot(images["board"].copy(), 0, range(1, 9))
    return {key: vision.Frame(image, ocr) for key, image in images.items()}
