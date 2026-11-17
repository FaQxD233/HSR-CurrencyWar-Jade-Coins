"""画面识别：OCR 封装，以及货币战争棋盘和黑市的读取。

所有坐标都是 1920×1080 设计坐标：截图先缩放到这个尺寸再识别，所以任何 16:9 分辨率都适用。
坐标来源：2560×1440 实机截图换算，并参考了 March7thAssistant 的 S2 界面坐标。
"""
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cached_property, lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

DESIGN_SIZE = (1920, 1080)

BOARD, MARKET, DETAIL, UNKNOWN = "棋盘", "黑市", "角色详情", "未知画面"

# 黑市（标题「黑市主理人」）
MARKET_TITLE = (962, 59, 1095, 92)
MARKET_REFRESH = (911, 512)                              # 「刷新轮换商品」按钮
CARD_COLUMNS = ((540, 757), (792, 1009), (1044, 1260))   # 三张轮换商品卡的左右边界
CARD_NAME_TOP, CARD_NAME_BOTTOM = 386, 424
CARD_BUY_Y = 285                                         # 商品图标中心，点击即购买
CARDS_AREA = (540, 175, 1260, 430)                       # 用像素变化判断刷新是否生效
DIALOG_AREA = (480, 250, 1440, 830)                      # 购买确认框可能出现的区域
CONFIRM_LABELS = ("确认", "确定", "购买", "确认购买")

# 棋盘
SHOP_LABEL = (1597, 965, 1649, 998)                      # 右下角「商店」
GOLD_DIGITS = (1612, 895, 1690, 945)                     # 右下角金币数（黑市打开时也可见）
BOND_LIST = (90, 220, 330, 790)                          # 左侧羁绊列表
MARKET_BUTTON_OFFSET = (83, 13)                          # 羁绊「黑市主理人」字样中心 → 右侧圆形入口
CONSUMABLES_AREA = (1540, 110, 1910, 210)                # 右上角道具栏，完美投影仪在这里
BOARD_AREA = (500, 300, 1450, 760)                       # 前台和后台两排角色（不含备战席）
BENCH_POINTS = tuple((440 + 125 * i, 912) for i in range(9))  # 备战席 9 格的中心
BENCH_AREA = (370, 850, 1510, 980)
SELL_POINT = (90, 915)                                   # 拖动角色时左下角出现的「出售」区（垃圾桶）
DETAIL_SELL_LABEL = (1735, 896, 1820, 937)               # 角色详情面板右下角的「出售」
CLOSE_POINT = (12, 12)                                   # 点左上角空白处，关闭黑市或角色详情

# 两个模板取自 shots/屏幕截图 2026-11-17 225129.png 缩放到设计尺寸后的画面：
# projector.png = (1662,135)-(1708,178)，只取图标中间，避开右下角的数量和上方的提示气泡；
# jade_face.png = (1140,380)-(1200,435)，翡翠卡面的脸部，避开底部星级和右上角的前后台标记。
ASSETS = Path(__file__).resolve().parent / "assets"
ICON_MATCH = 0.8          # 投影仪：有图标时约 1.0，没有时约 0.35
FACE_MATCH = 0.7          # 翡翠：棋盘上约 1.0，备战席复制品约 0.82，其他角色不超过 0.55
FACE_SCALES = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15)   # 前排、后排、备战席的卡面大小不同
BENCH_OCCUPIED_STD = 30   # 备战席空格的灰度标准差约 2，有角色约 65

REST_POINT = (1300, 520)                                 # 点击后鼠标停靠处，避免悬停提示挡住识别区域


@dataclass(frozen=True)
class Label:
    text: str
    x: float
    y: float


@dataclass(frozen=True)
class Card:
    index: int
    name: str

    @property
    def buy_point(self) -> tuple[int, int]:
        left, right = CARD_COLUMNS[self.index]
        return (left + right) // 2, CARD_BUY_Y


def normalize(text: str) -> str:
    return re.sub(r"[\s·•.,，。:：;；|'\"`「」【】()（）]", "", text)


def similar(text: str, target: str, threshold: float = 0.8) -> bool:
    """OCR 容错匹配：包含目标，或相似度达到阈值。错 1 个字仍能匹配，而「员工投影仪」不会匹配「完美投影仪」。"""
    text = normalize(text)
    return bool(text) and (target in text or SequenceMatcher(None, text, target).ratio() >= threshold)


def to_screen(point: tuple[float, float], rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """设计坐标 → 屏幕坐标。rect 是游戏客户区 (left, top, width, height)。"""
    left, top, width, height = rect
    return round(left + point[0] * width / DESIGN_SIZE[0]), round(top + point[1] * height / DESIGN_SIZE[1])


@lru_cache(maxsize=None)
def _template(name: str) -> np.ndarray:
    return np.asarray(Image.open(ASSETS / name).convert("RGB"))


def _match(rgb: np.ndarray, template: np.ndarray, area, scales=(1.0,)) -> tuple[float, tuple[int, int]]:
    """在区域内做（多尺度）模板匹配，返回最高分及匹配中心的设计坐标。"""
    left, top, right, bottom = area
    region = rgb[top:bottom, left:right]
    best = (-1.0, (0, 0))
    for scale in scales:
        tpl = template if scale == 1 else cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if tpl.shape[0] > region.shape[0] or tpl.shape[1] > region.shape[1]:
            continue
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED))
        if score > best[0]:
            best = (score, (left + x + tpl.shape[1] // 2, top + y + tpl.shape[0] // 2))
    return best


def _crop(rgb: np.ndarray, area, scale: float) -> np.ndarray:
    left, top, right, bottom = area
    part = rgb[top:bottom, left:right]
    if scale != 1:
        part = cv2.resize(part, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(part, cv2.COLOR_RGB2BGR)  # RapidOCR 把 ndarray 输入当作 BGR


class Ocr:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR  # 模型加载约 1 秒，用到时再导入
        self._engine = RapidOCR()

    def line(self, rgb: np.ndarray, area, scale: float = 2) -> str:
        """只识别不检测，适合位置固定的单行短文本，每次约 10 毫秒。"""
        result, _ = self._engine(_crop(rgb, area, scale), use_det=False, use_cls=False)
        return normalize(result[0][0]) if result else ""

    def labels(self, rgb: np.ndarray, area, scale: float = 1) -> list[Label]:
        """检测加识别，返回各文字框中心的设计坐标。较慢（0.3~1 秒），只在必要时用。"""
        result, _ = self._engine(_crop(rgb, area, scale))
        labels = []
        for box, text, _score in result or ():
            xs, ys = zip(*box)
            labels.append(Label(normalize(text), area[0] + sum(xs) / 4 / scale, area[1] + sum(ys) / 4 / scale))
        return labels


class Frame:
    """一帧游戏画面（已缩放到设计尺寸）。识别结果按需计算并缓存。"""

    def __init__(self, image: Image.Image, ocr: Ocr):
        if image.size != DESIGN_SIZE:
            image = image.resize(DESIGN_SIZE, Image.LANCZOS)
        self.image = image
        self.rgb = np.asarray(image)
        self._ocr = ocr

    @cached_property
    def page(self) -> str:
        if similar(self._ocr.line(self.rgb, MARKET_TITLE), "黑市主理人"):
            return MARKET
        # 角色详情打开时右下角「商店」仍然可见，所以要先于棋盘判断
        if similar(self._ocr.line(self.rgb, DETAIL_SELL_LABEL), "出售"):
            return DETAIL
        if similar(self._ocr.line(self.rgb, SHOP_LABEL), "商店"):
            return BOARD
        return UNKNOWN

    @cached_property
    def cards(self) -> tuple[Card, ...]:
        """黑市的三张轮换商品（名字区域右侧留出金币图标和价格）。"""
        return tuple(
            Card(index, self._ocr.line(self.rgb, (left + 5, CARD_NAME_TOP, right - 82, CARD_NAME_BOTTOM)))
            for index, (left, right) in enumerate(CARD_COLUMNS)
        )

    def price(self, card: Card) -> int | None:
        """商品价格。个位数价格在整屏检测时会漏，所以单独放大裁剪；金币图标可能被读成 @，只取末尾数字。"""
        right = CARD_COLUMNS[card.index][1]
        labels = sorted(self._ocr.labels(self.rgb, (right - 90, 380, right - 4, 428), scale=2), key=lambda l: l.x)
        for label in reversed(labels):
            if match := re.search(r"(\d{1,3})$", label.text):
                return int(match.group(1))
        return None

    @cached_property
    def gold(self) -> int | None:
        if self.page == DETAIL:  # 角色详情盖住了金币栏，那个位置显示的是出售价
            return None
        match = re.search(r"(\d{1,4})$", self._ocr.line(self.rgb, GOLD_DIGITS))
        return int(match.group(1)) if match else None

    def market_button(self) -> tuple[int, int] | None:
        """棋盘左侧羁绊列表里「黑市主理人」右边的圆形入口。羁绊排序会变，所以每次按文字定位。"""
        for label in self._ocr.labels(self.rgb, BOND_LIST):
            if similar(label.text, "黑市主理人"):
                return round(label.x + MARKET_BUTTON_OFFSET[0]), round(label.y + MARKET_BUTTON_OFFSET[1])
        return None

    @cached_property
    def projector(self) -> tuple[int, int] | None:
        """道具栏里的完美投影仪图标。位置随持有的道具种类变化，所以用模板找。"""
        score, point = _match(self.rgb, _template("projector.png"), CONSUMABLES_AREA)
        return point if score >= ICON_MATCH else None

    def projector_count(self) -> int | None:
        """投影仪图标右下角的数量；没有图标时为 0，数字读不出来时为 None。"""
        if self.projector is None:
            return 0
        x, y = self.projector
        text = self._ocr.line(self.rgb, (x + 20, y + 18, x + 46, y + 46), scale=3)
        return int(text) if re.fullmatch(r"\d{1,2}", text) else None

    @cached_property
    def jade(self) -> tuple[int, int] | None:
        """前台或后台的翡翠卡面位置（只搜两排棋盘，不含备战席）。"""
        score, point = _match(self.rgb, _template("jade_face.png"), BOARD_AREA, FACE_SCALES)
        return point if score >= FACE_MATCH else None

    @cached_property
    def _gray(self) -> np.ndarray:
        return cv2.cvtColor(self.rgb, cv2.COLOR_RGB2GRAY)

    def bench_occupied(self, index: int) -> bool:
        x, y = BENCH_POINTS[index]
        return float(self._gray[y - 50:y + 40, x - 38:x + 38].std()) >= BENCH_OCCUPIED_STD

    def empty_bench(self) -> list[int]:
        return [index for index in range(len(BENCH_POINTS)) if not self.bench_occupied(index)]

    def jade_on_bench(self, index: int) -> bool:
        x, y = BENCH_POINTS[index]
        score, _ = _match(self.rgb, _template("jade_face.png"), (x - 62, y - 62, x + 62, y + 63), FACE_SCALES)
        return score >= FACE_MATCH

    def confirm_button(self) -> tuple[int, int] | None:
        for label in self._ocr.labels(self.rgb, DIALOG_AREA):
            if label.text in CONFIRM_LABELS:
                return round(label.x), round(label.y)
        return None

    def diff(self, other: "Frame", area) -> float:
        """两帧在某区域内的平均像素差。"""
        left, top, right, bottom = area
        a = self.rgb[top:bottom, left:right].astype(np.int16)
        b = other.rgb[top:bottom, left:right].astype(np.int16)
        return float(np.abs(a - b).mean())
