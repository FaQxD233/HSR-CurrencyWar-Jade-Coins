"""测试替身：用实机截图模拟游戏画面，按点击 / 拖动的位置切换到下一张截图。"""
import threading

import vision
from jade_farm import Stop

POINTS = [
    ("refresh", vision.MARKET_REFRESH),
    ("market_button", (245, 347)),  # 第一张棋盘截图里的黑市入口
    ("market_button", (246, 429)),  # 最新棋盘截图里的黑市入口
    ("close", vision.CLOSE_POINT),
    ("projector", (1685, 156)),
    ("jade", (1170, 407)),
    ("sell", vision.SELL_POINT),
    *((f"card{i}", vision.Card(i, "").buy_point) for i in range(3)),
    *((f"bench{i}", point) for i, point in enumerate(vision.BENCH_POINTS)),
]

# 一个完整循环：开黑市 → 刷新 → 买 → 关黑市 → 用投影仪 → 卖复制品 → 再开黑市
LOOP = {
    ("board_done", "market_button"): "market",
    ("market", "refresh"): "target",
    ("target", "card2"): "market",                # 买走后第 3 格换成了别的商品
    ("market", "close"): "board_ready",           # 关掉黑市，投影仪在道具栏里
    ("board_ready", "projector->jade"): "board",  # 备战席第 1 格出现翡翠复制品
    ("board", "bench0->sell"): "board_done",      # 卖掉复制品；这里让投影仪也正好用完
}


def name_of(point):
    name, (x, y) = min(POINTS, key=lambda item: (item[1][0] - point[0]) ** 2 + (item[1][1] - point[1]) ** 2)
    assert abs(x - point[0]) <= 10 and abs(y - point[1]) <= 10, f"操作了意料之外的位置 {point}"
    return name


class FakeGame:
    """按操作切换截图的假游戏；操作次数到 limit 后再操作就抛出 Stop，用来结束无限循环。"""

    def __init__(self, frames, start, transitions, limit=None):
        self.frames, self.state, self.transitions, self.limit = frames, start, transitions, limit
        self.actions = []

    def frame(self):
        return self.frames[self.state]

    def ready(self):
        return True

    def click(self, point):
        self._act(name_of(point))

    def drag(self, start, end):
        self._act(f"{name_of(start)}->{name_of(end)}")

    def _act(self, action):
        if self.limit is not None and len(self.actions) >= self.limit:
            raise Stop
        self.actions.append(action)
        self.state = self.transitions.get((self.state, action), self.state)


class FakeControls:
    """Farm 一暂停，下一个检查点就抛出 Stop，测试随之结束。"""

    def __init__(self):
        self.paused = threading.Event()

    def stop_requested(self):
        return self.paused.is_set()

    def alert(self):
        pass
