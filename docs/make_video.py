"""生成「翡翠刷钱助手」功能介绍视频：实机截图 + 动画标注 + 字幕 + Windows 中文语音旁白。

用法：
    python docs/make_video.py              生成 docs/翡翠刷钱助手-介绍.mp4
    python docs/make_video.py --preview D  只把每个场景的几帧画面存成图片到目录 D，用来检查画面

依赖：ffmpeg（需在 PATH 里）、pywin32（调用系统语音合成）、Pillow。
素材：shots/ 里的实机截图（左下角 UID 会打码）、docs/gui.png（界面截图）。
"""
import argparse
import math
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

import win32com.client
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "shots"
GUI_IMAGE = ROOT / "docs" / "gui.png"
OUTPUT = ROOT / "docs" / "翡翠刷钱助手-介绍.mp4"

W, H, FPS = 1920, 1080, 30
RATE = 22050                       # 旁白采样率：SAPI 22kHz 16bit 单声道
LEAD, GAP, TAIL = 0.4, 0.2, 0.7    # 每个场景开头留白、句间停顿、结尾留白（秒）
FADE = 0.4                         # 场景切换的淡入时长（秒）
SPEECH_RATE = 2                    # 语速：-10～10
GOLD, WHITE, GREY = (255, 211, 77), (245, 245, 250), (170, 168, 190)
FONT_REGULAR, FONT_BOLD = "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"

SHOT_FILES = {
    "board": "屏幕截图 2026-11-17 225129.png",   # 棋盘：投影仪 3 个，复制品在备战席第 1 格
    "market": "屏幕截图 2026-11-17 220632.png",  # 黑市：没有目标
    "target": "屏幕截图 2026-11-17 220649.png",  # 黑市：第 3 格是完美投影仪
    "drag": "屏幕截图 2026-11-17 225057.png",    # 拖动复制品时出现的出售区
}
SOURCE_SCALE = 2560 / W            # 截图是 2560×1440，标注坐标沿用程序里的 1920×1080 设计坐标
UID_BOX = (28, 1036, 180, 1070)

# 程序里用到的界面位置（设计坐标，与 vision.py 一致）
REFRESH_BOX = (838, 494, 988, 532)
TARGET_CARD = (1044, 175, 1260, 430)
BUY_POINT = (1152, 285)
MARKET_ENTRY = (100, 398, 274, 462)
MARKET_BUTTON = (246, 430)
PROJECTOR_BOX = (1655, 122, 1730, 198)
JADE_CARD = (1113, 343, 1237, 467)
BENCH0 = (378, 850, 502, 975)
SELL_ZONE = (0, 806, 176, 1035)
SELL_POINT = (90, 915)

# 界面截图（docs/gui.png，713×808）里各区域的位置
GUI_PARTS = [
    ("状态", "自动检测游戏窗口和识别模型", (10, 4, 705, 118)),
    ("设置", "每次买几次、保底金币、刷新上限", (10, 130, 705, 300)),
    ("统计", "已买、已卖、当前金币、用时", (10, 312, 705, 380)),
    ("按钮", "开始 / 暂停 / 停止 / 诊断", (10, 390, 520, 430)),
    ("日志", "每一步操作都有记录", (10, 470, 705, 800)),
]


# ---------- 基础工具 ----------

@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * x)


def phase(t: float, start: float, end: float) -> float:
    """t 在 [start, end] 之间时从 0 平滑过渡到 1。"""
    return ease((t - start) / max(end - start, 1e-6))


def lerp(a, b, k):
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def scale_box(box):
    return tuple(round(v * SOURCE_SCALE) for v in box)


@lru_cache(maxsize=None)
def shot(name: str) -> Image.Image:
    image = Image.open(SHOTS / SHOT_FILES[name]).convert("RGB").resize((2560, 1440))
    uid = scale_box(UID_BOX)
    image.paste(image.crop(uid).filter(ImageFilter.GaussianBlur(14)), uid[:2])
    return image


@lru_cache(maxsize=None)
def board_without_copy() -> Image.Image:
    """把棋盘截图里备战席第 1 格换成空格，表示用投影仪之前的样子。"""
    image = shot("board").copy()
    empty = scale_box((BENCH0[0] + 125, BENCH0[1], BENCH0[2] + 125, BENCH0[3]))
    image.paste(image.crop(empty), scale_box(BENCH0)[:2])
    return image


@lru_cache(maxsize=None)
def cutout(name: str, box) -> Image.Image:
    return (board_without_copy() if name == "board_ready" else shot(name)).crop(scale_box(box))


@lru_cache(maxsize=None)
def backdrop() -> Image.Image:
    """标题、界面、安全等场景用的深紫色渐变背景。"""
    top, bottom = (24, 18, 52), (58, 30, 92)
    column = Image.new("RGB", (1, H))
    for y in range(H):
        column.putpixel((0, y), tuple(round(a + (b - a) * y / H) for a, b in zip(top, bottom)))
    image = column.resize((W, H))
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse((W * 0.2, -H * 0.3, W * 0.8, H * 0.6), fill=70)
    return Image.composite(Image.new("RGB", (W, H), (120, 80, 200)), image, glow.filter(ImageFilter.GaussianBlur(160)))


@dataclass
class Camera:
    """镜头：中心点（设计坐标）和放大倍数。"""
    cx: float = W / 2
    cy: float = H / 2
    zoom: float = 1.0

    def box(self):
        w, h = W / self.zoom, H / self.zoom
        left = min(max(self.cx - w / 2, 0), W - w)
        top = min(max(self.cy - h / 2, 0), H - h)
        return left, top, w, h

    def point(self, p):
        left, top, _, _ = self.box()
        return (p[0] - left) * self.zoom, (p[1] - top) * self.zoom

    def rect(self, box):
        return (*self.point(box[:2]), *self.point(box[2:]))

    def render(self, image: Image.Image) -> Image.Image:
        left, top, w, h = self.box()
        s = SOURCE_SCALE
        return image.resize((W, H), Image.BILINEAR, box=(left * s, top * s, (left + w) * s, (top + h) * s))


def camera_move(t, start, end, a: Camera, b: Camera) -> Camera:
    k = phase(t, start, end)
    return Camera(*lerp((a.cx, a.cy, a.zoom), (b.cx, b.cy, b.zoom), k))


class Canvas:
    """一帧画面：底图 + 叠加层。标注坐标是设计坐标，经镜头换算到画面上。"""

    def __init__(self, base: Image.Image, camera: Camera | None = None):
        self.camera = camera or Camera()
        self.base = base.convert("RGBA")
        self.layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.layer)

    @classmethod
    def from_shot(cls, image: Image.Image, camera: Camera) -> "Canvas":
        return cls(camera.render(image), camera)

    def spotlight(self, box, strength: float, pad: int = 10) -> None:
        """压暗框外区域，突出框内。"""
        if strength <= 0:
            return
        x1, y1, x2, y2 = self.camera.rect(box)
        mask = Image.new("L", (W, H), round(165 * strength))
        ImageDraw.Draw(mask).rounded_rectangle((x1 - pad, y1 - pad, x2 + pad, y2 + pad), radius=16, fill=0)
        shade = Image.new("RGBA", (W, H), (8, 4, 20, 255))
        shade.putalpha(mask)
        self.base = Image.alpha_composite(self.base, shade)

    def outline(self, box, alpha: float, label: str | None = None, pad: int = 10) -> None:
        if alpha <= 0:
            return
        x1, y1, x2, y2 = self.camera.rect(box)
        rect = (x1 - pad, y1 - pad, x2 + pad, y2 + pad)
        for width, a in ((16, 50), (6, 255)):
            self.draw.rounded_rectangle(rect, radius=16, outline=(*GOLD, round(a * alpha)), width=width)
        if label:
            self.tag(label, (rect[0] + rect[2]) / 2, rect[1] - 34, alpha, screen=True)

    def tag(self, text: str, x: float, y: float, alpha: float, *, size: int = 38, screen: bool = False) -> None:
        """金色底的小标签，(x, y) 是标签中心。"""
        if alpha <= 0:
            return
        if not screen:
            x, y = self.camera.point((x, y))
        f = font(size, bold=True)
        left, top, right, bottom = self.draw.textbbox((0, 0), text, font=f)
        w, h = right - left + 36, bottom - top + 20
        x = min(max(x, w / 2 + 12), W - w / 2 - 12)
        y = min(max(y, h / 2 + 12), H - h / 2 - 12)
        self.draw.rounded_rectangle((x - w / 2, y - h / 2, x + w / 2, y + h / 2), radius=h / 2,
                                    fill=(*GOLD, round(235 * alpha)))
        self.draw.text((x, y), text, font=f, fill=(30, 20, 50, round(255 * alpha)), anchor="mm")

    def ripple(self, p, k: float) -> None:
        """点击波纹，k 从 0 到 1。"""
        if not 0 < k < 1:
            return
        x, y = self.camera.point(p)
        for i, scale in enumerate((1.0, 0.6)):
            r = (18 + 70 * k) * scale
            self.draw.ellipse((x - r, y - r, x + r, y + r), outline=(*WHITE, round(230 * (1 - k))), width=6 - 2 * i)
        self.draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=(*GOLD, round(255 * (1 - k))))

    def arrow(self, p0, p1, k: float) -> None:
        """从 p0 指向 p1 的箭头，k 是画出来的进度。"""
        if k <= 0:
            return
        a, b = self.camera.point(p0), self.camera.point(p1)
        end = lerp(a, b, k)
        self.draw.line((*a, *end), fill=(*GOLD, 235), width=10)
        angle = math.atan2(end[1] - a[1], end[0] - a[0])
        head = [end, (end[0] - 34 * math.cos(angle - 0.45), end[1] - 34 * math.sin(angle - 0.45)),
                (end[0] - 34 * math.cos(angle + 0.45), end[1] - 34 * math.sin(angle + 0.45))]
        self.draw.polygon(head, fill=(*GOLD, 255))

    def sprite(self, image: Image.Image, center, *, scale: float = 1.0, alpha: float = 1.0, screen=False) -> None:
        """在 center 处贴一张小图（截图里裁下来的道具或角色卡）。"""
        x, y = center if screen else self.camera.point(center)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        sprite = image.resize(size, Image.BILINEAR).convert("RGBA")
        if alpha < 1:
            sprite.putalpha(sprite.getchannel("A").point(lambda v: round(v * alpha)))
        self.layer.alpha_composite(sprite, (round(x - size[0] / 2), round(y - size[1] / 2)))

    def text(self, text: str, xy, size: int, *, color=WHITE, alpha: float = 1.0, bold=False, anchor="mm") -> None:
        if alpha > 0:
            self.draw.text(xy, text, font=font(size, bold), fill=(*color, round(255 * alpha)), anchor=anchor)

    def panel(self, box, alpha: float) -> None:
        if alpha > 0:
            self.draw.rounded_rectangle(box, radius=24, fill=(14, 10, 32, round(215 * alpha)),
                                        outline=(*GOLD, round(160 * alpha)), width=3)

    def image(self) -> Image.Image:
        return Image.alpha_composite(self.base, self.layer).convert("RGB")


def crossfade(a: Image.Image, b: Image.Image, k: float) -> Image.Image:
    return b if k >= 1 else a if k <= 0 else Image.blend(a, b, k)


# ---------- 场景 ----------

@dataclass
class Scene:
    sentences: list[str]
    draw: Callable[[float, float], Image.Image]          # (场景内时间, 场景时长) → 画面
    clips: list[tuple[Path, float]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return LEAD + sum(seconds for _, seconds in self.clips) + GAP * (len(self.clips) - 1) + TAIL

    def caption(self, t: float) -> str | None:
        start = LEAD
        for sentence, (_, seconds) in zip(self.sentences, self.clips):
            if t < start + seconds + GAP:
                return sentence if t >= start - 0.15 else None
            start += seconds + GAP
        return None


def title_scene(t, d):
    c = Canvas(backdrop())
    k = phase(t, 0, 0.8)
    c.text("货币战争 · 三星翡翠", (W / 2, 330 + 30 * (1 - k)), 84, bold=True, alpha=k)
    c.text("自动刷钱助手", (W / 2, 460 + 30 * (1 - k)), 128, color=GOLD, bold=True, alpha=k)
    c.text("自动刷黑市  ·  复制翡翠  ·  自动出售", (W / 2, 610), 46, color=GREY, alpha=phase(t, 0.6, 1.4))
    float_y = 10 * math.sin(t * 2.2)
    c.sprite(cutout("board", JADE_CARD), (330, 470 + float_y), scale=1.7, alpha=phase(t, 0.3, 1.1), screen=True)
    c.sprite(cutout("target", (1105, 238, 1200, 333)), (1590, 470 - float_y), scale=2.1, alpha=phase(t, 0.3, 1.1),
             screen=True)
    return c.image()


def principle_scene(t, d):
    cam = camera_move(t, 0, 1.6, Camera(), Camera(900, 420, 1.25))
    c = Canvas.from_shot(shot("target"), cam)
    k = phase(t, 0.8, 1.6)
    c.spotlight(TARGET_CARD, k)
    c.outline(TARGET_CARD, k, "完美投影仪：3 个 9 金币")
    c.panel((90, 260, 900, 700), phase(t, 0.30 * d, 0.38 * d))
    lines = [("3 个完美投影仪 = 9 金币", WHITE, 46, 0.36), ("复制 3 个翡翠 × 14 金币 = 42", WHITE, 46, 0.52),
             ("每组净赚 33 金币", GOLD, 66, 0.70)]
    for i, (line, color, size, start) in enumerate(lines):
        c.text(line, (495, 360 + i * 130), size, color=color, bold=i == 2, alpha=phase(t, start * d, start * d + 0.5))
    return c.image()


def entry_scene(t, d):
    cam = camera_move(t, 0, 1.4, Camera(), Camera(420, 430, 1.6))
    c = Canvas.from_shot(shot("board"), cam)
    k = phase(t, 0.9, 1.6)
    c.spotlight(MARKET_ENTRY, k)
    c.outline(MARKET_ENTRY, k, "黑市入口")
    c.ripple(MARKET_BUTTON, (t - 0.55 * d) / 0.7)
    return c.image()


def refresh_buy_scene(t, d):
    cam = Camera(900, 380, 1.3)
    switch = phase(t, 0.46 * d, 0.46 * d + 0.5)
    first = Canvas.from_shot(shot("market"), cam)
    k = phase(t, 0.2, 0.8)
    first.spotlight(REFRESH_BOX, k)
    first.outline(REFRESH_BOX, k, "免费刷新")
    for start in (0.12, 0.28):
        first.ripple(((REFRESH_BOX[0] + REFRESH_BOX[2]) / 2, (REFRESH_BOX[1] + REFRESH_BOX[3]) / 2),
                     (t - start * d) / 0.7)
    if switch <= 0:
        return first.image()
    second = Canvas.from_shot(shot("target"), cam)
    k = phase(t, 0.5 * d, 0.5 * d + 0.6)
    second.spotlight(TARGET_CARD, k)
    second.outline(TARGET_CARD, k, "识别到「完美投影仪」")
    second.ripple(BUY_POINT, (t - 0.72 * d) / 0.7)
    second.tag("已买下  −9 金币", 1152, 470, phase(t, 0.8 * d, 0.8 * d + 0.4))
    return crossfade(first.image(), second.image(), switch)


def copy_scene(t, d):
    cam = Camera(1300, 420, 1.15)
    move = phase(t, 0.15 * d, 0.55 * d)
    switch = phase(t, 0.6 * d, 0.6 * d + 0.5)
    first = Canvas.from_shot(board_without_copy(), cam)
    first.outline(PROJECTOR_BOX, phase(t, 0.2, 0.7), "完美投影仪", pad=6)
    start = ((PROJECTOR_BOX[0] + PROJECTOR_BOX[2]) / 2, (PROJECTOR_BOX[1] + PROJECTOR_BOX[3]) / 2)
    jade = ((JADE_CARD[0] + JADE_CARD[2]) / 2, (JADE_CARD[1] + JADE_CARD[3]) / 2)
    first.arrow(start, jade, move)
    first.outline(JADE_CARD, phase(t, 0.5 * d, 0.5 * d + 0.4), "翡翠")
    if 0 < move < 1:
        first.sprite(cutout("board", PROJECTOR_BOX), lerp(start, jade, move), scale=cam.zoom * 1.1, alpha=0.9)
    if switch <= 0:
        return first.image()
    second = Canvas.from_shot(shot("board"), Camera(900, 640, 1.1))
    k = phase(t, 0.68 * d, 0.68 * d + 0.5)
    second.spotlight(BENCH0, k)
    second.outline(BENCH0, k, "翡翠复制品")
    return crossfade(first.image(), second.image(), switch)


def sell_scene(t, d):
    cam = Camera(420, 800, 1.35)
    c = Canvas.from_shot(shot("drag"), cam)
    k = phase(t, 0.2, 0.8)
    c.spotlight((SELL_ZONE[0], SELL_ZONE[1], BENCH0[2], SELL_ZONE[3]), k * 0.8)
    c.outline(SELL_ZONE, k * (0.75 + 0.25 * math.sin(t * 6)), "出售区", pad=4)
    start = ((BENCH0[0] + BENCH0[2]) / 2, (BENCH0[1] + BENCH0[3]) / 2)
    move = phase(t, 0.15 * d, 0.48 * d)
    c.arrow(start, SELL_POINT, move)
    if 0 < move < 1:
        c.sprite(cutout("board", BENCH0), lerp(start, SELL_POINT, move), scale=cam.zoom * 0.9, alpha=0.85)
    rise = phase(t, 0.5 * d, 0.5 * d + 1.2)
    c.tag("+14 金币", SELL_POINT[0] + 30, SELL_POINT[1] - 120 - 40 * rise, min(rise * 3, 1.0), size=44)
    return c.image()


@lru_cache(maxsize=None)
def gui_image() -> Image.Image:
    image = Image.open(GUI_IMAGE).convert("RGB")
    return image.resize((round(image.width * 900 / image.height), 900), Image.LANCZOS)


def gui_scene(t, d):
    c = Canvas(backdrop())
    gui = gui_image()
    scale = gui.height / Image.open(GUI_IMAGE).height
    origin = (150, 26)
    c.sprite(gui, (origin[0] + gui.width / 2, origin[1] + gui.height / 2), alpha=phase(t, 0, 0.6), screen=True)
    active = min(int(t / (0.92 * d / len(GUI_PARTS))), len(GUI_PARTS) - 1)
    for i, (name, desc, box) in enumerate(GUI_PARTS):
        y = 170 + i * 150
        on = i == active
        appear = phase(t, 0.3 + i * 0.25, 0.8 + i * 0.25)
        c.text(name, (1080, y), 52, color=GOLD if on else WHITE, bold=True, alpha=appear, anchor="lm")
        c.text(desc, (1080, y + 58), 34, color=WHITE if on else GREY, alpha=appear, anchor="lm")
        if on:
            x1, y1, x2, y2 = (origin[0] + box[0] * scale, origin[1] + box[1] * scale,
                              origin[0] + box[2] * scale, origin[1] + box[3] * scale)
            c.draw.rounded_rectangle((x1, y1, x2, y2), radius=12, outline=(*GOLD, 255), width=6)
            c.draw.line((x2 + 6, (y1 + y2) / 2, 1060, y + 20), fill=(*GOLD, 200), width=4)
    return c.image()


SAFETY = ["只卖新出现的翡翠复制品，棋盘上的本体不会动",
          "认不出画面：自动暂停、响铃、保存截图",
          "游戏切到后台：自动等待，切回来再继续",
          "游戏里 F10 停止，F11 暂停 / 继续"]


def safety_scene(t, d):
    c = Canvas(backdrop())
    c.text("安全设计", (W / 2, 160), 72, color=GOLD, bold=True, alpha=phase(t, 0, 0.6))
    for i, line in enumerate(SAFETY):
        k = phase(t, 0.15 * d + i * 0.17 * d, 0.15 * d + i * 0.17 * d + 0.5)
        y = 320 + i * 130
        x = 420 - 40 * (1 - k)
        c.draw.ellipse((x - 30, y - 30, x + 30, y + 30), fill=(*GOLD, round(255 * k)))
        c.draw.line((x - 14, y + 1, x - 4, y + 12, x + 15, y - 11), fill=(30, 20, 50, round(255 * k)), width=7,
                    joint="curve")  # 对勾（微软雅黑里没有 ✓ 字形，直接画）
        c.text(line, (x + 60, y), 46, alpha=k, anchor="lm")
    return c.image()


def ending_scene(t, d):
    c = Canvas(backdrop())
    k = phase(t, 0, 0.8)
    c.text("翡翠刷钱助手", (W / 2, 400), 120, color=GOLD, bold=True, alpha=k)
    c.text("双击「翡翠刷钱助手.pyw」开始使用", (W / 2, 560), 48, alpha=phase(t, 0.4, 1.0))
    c.text("仅供学习交流", (W / 2, 840), 30, color=GREY, alpha=phase(t, 0.8, 1.4))
    return c.image()


SCENES = [
    Scene(["这是货币战争的三星翡翠自动刷钱助手。", "一分钟带你看懂它能做什么。"], title_scene),
    Scene(["翡翠的黑市会刷出完美投影仪，三个只要九金币。", "用在翡翠身上能复制出翡翠，每个复制品卖十四金币。",
           "所以每买一组，净赚三十三金币。"], principle_scene),
    Scene(["第一步，点击左侧「黑市主理人」旁边的圆形图标，打开黑市。"], entry_scene),
    Scene(["然后不停地免费刷新轮换商品。", "识别到完美投影仪就直接买下，每次进黑市买一到三次。"], refresh_buy_scene),
    Scene(["买完关掉黑市，把投影仪拖到翡翠身上。", "备战席上就会出现一个翡翠复制品。"], copy_scene),
    Scene(["复制品会被马上拖进左下角的出售区卖掉。", "投影仪用完后再回到黑市，一直循环。"], sell_scene),
    Scene(["双击「翡翠刷钱助手」就能打开操作界面。", "设置好每次买几次和保底金币，点开始，再切回游戏就行。",
           "收益和日志实时显示；不放心可以先点诊断检查识别。"], gui_scene),
    Scene(["程序只卖新出现的翡翠复制品。", "认不出画面就自动暂停、响铃并保存截图。",
           "游戏里按 F10 停止，按 F11 暂停或继续。"], safety_scene),
    Scene(["祝你刷钱愉快！"], ending_scene),
]


# ---------- 旁白与字幕 ----------

def synthesize(scenes: list[Scene], work: Path) -> None:
    """每句话单独合成语音，记录时长，字幕据此逐句对齐。"""
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    tokens = voice.GetVoices()
    chinese = [tokens.Item(i) for i in range(tokens.Count) if "Chinese" in tokens.Item(i).GetDescription()]
    if not chinese:
        raise SystemExit("系统里没有中文语音（如 Microsoft Huihui），无法生成旁白")
    voice.Voice = chinese[0]
    voice.Rate = SPEECH_RATE
    audio_format = win32com.client.Dispatch("SAPI.SpAudioFormat")
    audio_format.Type = 22  # SAFT22kHz16BitMono
    for s, scene in enumerate(scenes):
        scene.clips = []
        for i, sentence in enumerate(scene.sentences):
            path = work / f"{s:02d}-{i}.wav"
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Format = audio_format
            stream.Open(str(path), 3, False)
            voice.AudioOutputStream = stream
            voice.Speak(sentence.replace("F10", "F十").replace("F11", "F十一"), 0)
            stream.Close()
            with wave.open(str(path)) as clip:
                scene.clips.append((path, clip.getnframes() / clip.getframerate()))


def frame_count(scene: Scene) -> int:
    return round(scene.duration * FPS)


def build_audio(scenes: list[Scene], path: Path) -> None:
    """按场景拼接旁白，每个场景的音频长度与它的视频帧数严格对齐。"""
    def silence(seconds):
        return b"\x00\x00" * round(seconds * RATE)

    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        for scene in scenes:
            data = bytearray(silence(LEAD))
            for i, (clip, _) in enumerate(scene.clips):
                with wave.open(str(clip)) as w:
                    data += w.readframes(w.getnframes())
                if i < len(scene.clips) - 1:
                    data += silence(GAP)
            size = round(frame_count(scene) / FPS * RATE) * 2
            out.writeframes(bytes(data[:size]) + b"\x00" * max(0, size - len(data)))


def add_caption(image: Image.Image, text: str | None) -> Image.Image:
    if not text:
        return image
    canvas = image.convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    size = 44
    while size > 30 and draw.textlength(text, font=font(size)) > W - 240:
        size -= 2
    width = draw.textlength(text, font=font(size))
    draw.rounded_rectangle((W / 2 - width / 2 - 40, 948, W / 2 + width / 2 + 40, 1040), radius=20, fill=(8, 6, 18, 190))
    draw.text((W / 2, 994), text, font=font(size), fill=(*WHITE, 255), anchor="mm")
    return Image.alpha_composite(canvas, layer).convert("RGB")


def frames(scenes: list[Scene]):
    previous = None
    for scene in scenes:
        d = scene.duration
        for n in range(frame_count(scene)):
            t = n / FPS
            image = scene.draw(t, d)
            if previous is not None and t < FADE:
                image = crossfade(previous, image, phase(t, 0, FADE))
            last = image
            yield add_caption(image, scene.caption(t))
        previous = last


def main() -> None:
    parser = argparse.ArgumentParser(description="生成翡翠刷钱助手的功能介绍视频")
    parser.add_argument("--preview", type=Path, help="只输出每个场景的几帧画面到这个目录，不生成视频")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        synthesize(SCENES, work)
        total = sum(scene.duration for scene in SCENES)
        print(f"旁白合成完成：{len(SCENES)} 个场景，共 {total:.1f} 秒")
        if args.preview:
            args.preview.mkdir(parents=True, exist_ok=True)
            for s, scene in enumerate(SCENES):
                for k in (0.25, 0.55, 0.85):
                    t = scene.duration * k
                    add_caption(scene.draw(t, scene.duration), scene.caption(t)).save(
                        args.preview / f"scene{s + 1}-{round(k * 100)}.png")
            print(f"预览画面已保存到 {args.preview}")
            return
        audio = work / "narration.wav"
        build_audio(SCENES, audio)
        command = ["ffmpeg", "-y", "-loglevel", "error",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                   "-i", str(audio),
                   "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(OUTPUT)]
        encoder = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for i, image in enumerate(frames(SCENES)):
                encoder.stdin.write(image.tobytes())
                if i % (FPS * 10) == 0:
                    print(f"  已渲染 {i / FPS:.0f} / {total:.0f} 秒", flush=True)
        finally:
            encoder.stdin.close()
            encoder.wait()
        if encoder.returncode != 0:
            raise SystemExit(f"ffmpeg 编码失败（返回码 {encoder.returncode}）")
        print(f"视频已生成：{OUTPUT}")


if __name__ == "__main__":
    main()
