# -*- coding: utf-8 -*-
"""抢单核心：对时、预热、定时按优先级依次提交

设计要点
1. 不轮询。只在开抢那一刻开始发提交请求。
2. 用服务器 Date 头对齐时钟，不依赖本机时间准不准。
3. 场地 WID 跨日期、跨时段固定（已实测 28 片全部稳定），所以提前建一份
   永久索引 cache 到磁盘。开抢时连「查场地」这一步都省了，一次 POST 直接下单。
4. 首选场地被抢走时，立即按优先级试下一片。检测到限流立刻停手。
"""
from __future__ import annotations

import datetime as dt
import email.utils
import heapq
import json
import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from api import SzuClient, build_form, ApiError

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX_FILE = ROOT / "capture" / "court_index.json"
CLOCK_FILE = ROOT / "capture" / "clock.json"

Log = Callable[[str], None]

# 这些回复说明「这片没了，换下一片」，可以继续尝试
FULL_WORDS = ("已满", "满场", "已被预约", "已被占", "被占用", "已约满", "无可用", "已被预订")
# 这些回复说明「还没放场」，值得等几十毫秒再打一次
EARLY_WORDS = ("未开始", "尚未", "未开放", "未到", "不在可预约")
# 这些回复说明「账号被限流」或「已约过」，必须停手
STOP_WORDS = ("频繁", "稍后再试", "操作过快")

# 放场前几秒开始发只读查询。2 秒 = 十几枪（一次往返 150 毫秒），
# 足够盖住时钟误差，又短到不可能触发频繁。
FIRE_LEAD = 2.0

# 盯场时同时开几路只读查询。三路错开发出，等于每 9 毫秒就有一个请求
# 在问「放场了没有」，比单路快三倍；服务器实测 96 次/秒零错误。
LANES = 3
LANE_GAP = 0.01        # 相邻两路错开 10 毫秒，别同一瞬间一起冲


def _noop(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Plan:
    date: str                     # 2026-10-30
    slot: str                     # 19:00-20:00
    xmdm: str = "001"
    xqdm: str = "1"
    cgbm: str = ""
    yylx: str = "1.0"
    courts: list[str] = field(default_factory=list)      # 按优先级排好的场地名（A2/B4…）
    rows: dict[str, dict] = field(default_factory=dict)   # 场地名 -> 原始行（含 WID/CGBM/XMDM/XQDM）
    full: set[str] = field(default_factory=set)           # 实时查出「已经被别人约了」的场地
    free: set[str] = field(default_factory=set)           # 实时查出「还空着」的场地
    checked: bool = False                                 # 上面的结果是否真的查过
    pending: bool = False                                 # 目标日期还没到放场时刻（要等 12:30）
    not_open: bool = False                                # 上一次查询显示「整批还没放出来」

    def order(self) -> list[str]:
        """实际要依次提交的场地：跳过已经确认被约掉的。"""
        return [c for c in self.courts if c not in self.full]


def core_name(cdmc: str) -> str:
    return (cdmc or "").replace("羽毛球场", "").strip()


def split_rooms(rows: list[dict]) -> tuple[set[str], set[str], bool]:
    """把一次场地查询的结果拆成「还空着 / 已被占 / 整批是不是还没放场」。

    单独抽出来，是因为盯场时要三路并发查，每路都在自己的线程里跑。
    让它们各自解析、只把结论交给主线程，就不会互相覆盖 plan 里的字段。
    """
    free = {core_name(r.get("CDMC")) for r in rows if not r.get("disabled")}
    full = {core_name(r.get("CDMC")) for r in rows if r.get("disabled")}
    free.discard("")
    full.discard("")
    # 整批都标着「无开放场地」，说明还没到放场时刻，不是「被人抢光了」
    texts = " ".join(str(r.get("text") or "") for r in rows)
    not_open = any(w in texts for w in ("无开放", "尚未", "未开放", "未开始"))
    return free, full, not_open


def _index_key(xqdm: str, xmdm: str, cgbm: str) -> str:
    return f"{xqdm}|{xmdm}|{cgbm}"


def load_index() -> dict:
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_index(idx: dict) -> None:
    try:
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def save_offset(offset: float, err: float = 0.5) -> None:
    """把这次对时的结果存下来，下次来不及对时也能有个底"""
    try:
        CLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLOCK_FILE.write_text(json.dumps({"offset": offset, "err": err, "at": time.time()}),
                              encoding="utf-8")
    except Exception:
        pass


def load_clock(max_age: float = 86400.0) -> dict | None:
    try:
        d = json.loads(CLOCK_FILE.read_text(encoding="utf-8"))
        if 0 <= time.time() - float(d["at"]) <= max_age:
            return d
    except Exception:
        pass
    return None


def load_offset(max_age: float = 900.0) -> tuple[float, float] | None:
    """取上一次的对时结果，超过 15 分钟就当过期"""
    try:
        d = json.loads(CLOCK_FILE.read_text(encoding="utf-8"))
        off, at = float(d["offset"]), float(d["at"])
        age = time.time() - at
        if 0 <= age <= max_age:
            return off, age
    except Exception:
        pass
    return None


class BookingEngine:
    def __init__(self, client: SzuClient, log: Log = _noop,
                 on_fire: Callable[[], None] | None = None):
        self.c = client
        self.log = log
        self.on_fire = on_fire   # 每真实发出一次提交就通知一次，用于精确计数
        self.offset = 0.0          # 服务器时间 - 本地时间
        self.clock_err = 0.5       # 对时误差（秒），越大越不准
        self.user_id = ""
        self.user_name = ""

    # ---------- 时钟 ----------
    def sync_clock(self, samples: int = 14, gap: float = 0.24) -> float:
        """估算本机与服务器的时差。

        HTTP 的 Date 头只精确到整秒，单次采样误差可达 ±0.5 秒。
        这里用「区间求交」：每次采样得到一个整秒数，它就给出一个关于时差的
        约束区间，多次取交集即可收窄。精度取决于采样**跨了多长时间**——
        跨度要超过 1 秒、且采样间隔带抖动，小数部分才能铺满，实测能收窄到 ±50ms。

        但走代理时个别请求会被拖慢几百毫秒甚至超过 1 秒，让交集直接塌成空集。
        所以这里不求「全部样本的交集」，而是求**最大一致子集**的交集：
        允许丢掉几个被网络拖累的采样，用剩下的多数样本定位。
        """
        cons: list[tuple[float, float]] = []     # 每个样本给出的 (下界, 上界)
        for i in range(samples):
            t0 = time.time()
            try:
                r = self.c.s.get("https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy/index.do",
                                 timeout=8, allow_redirects=False)
                d = r.headers.get("Date")
                mid = (t0 + time.time()) / 2
                if d:
                    sec = email.utils.parsedate_to_datetime(d).timestamp()
                    # sec <= mid + offset < sec + 1
                    cons.append((sec - mid, sec + 1 - mid))
            except Exception:
                pass
            if i < samples - 1:
                time.sleep(gap + (0.06 if i % 3 == 0 else 0) + (i % 5) * 0.013)

        if not cons:
            self.log("对时失败，按本机时间执行")
            self.offset = 0.0
            return 0.0

        # 按区间的下界排序，用最小堆维护上界，找出同时满足最多样本的那个点
        cons.sort()
        heap: list[float] = []
        best_n, best_lo, best_hi = 0, cons[0][0], cons[0][1]
        for lo_i, hi_i in cons:
            heapq.heappush(heap, hi_i)
            while heap and heap[0] < lo_i:       # 上界已经落在当前下界之前的，踢掉
                heapq.heappop(heap)
            if len(heap) > best_n:
                best_n = len(heap)
                best_lo, best_hi = lo_i, heap[0]

        dropped = len(cons) - best_n
        if best_n >= 3 and best_hi > best_lo:
            self.offset = (best_lo + best_hi) / 2
            self.clock_err = (best_hi - best_lo) / 2
            tail = f"，丢掉 {dropped} 个抖动样本" if dropped else ""
            self.log(f"对时完成：本机与服务器相差 {self.offset:+.3f} 秒"
                     f"（误差 ±{self.clock_err * 1000:.0f}ms，{best_n} 次采样{tail}）")
        else:
            # 网络抖得太厉害，退回中位数：精度差些，但不会被单个异常值带跑
            mids = sorted((a + b) / 2 for a, b in cons)
            self.offset = mids[len(mids) // 2]
            self.clock_err = 0.5
            self.log(f"对时偏差较大（网络抖动），取中位数 {self.offset:+.3f} 秒，"
                     f"误差约 ±500ms，建议开抢前重试一次")
        save_offset(self.offset, self.clock_err)
        return self.offset

    def ensure_clock(self, deadline: float, min_time: float = 8.0) -> None:
        """在 deadline 之前把时差定下来，时间不够就别硬等。

        一次完整对时要 4 秒（14 次采样、跨度 3.9 秒，精度才有 ±50ms）。
        如果用户是掐着 12:30 前几秒才点的「开始抢单」，照走 4 秒就会错过整点，
        所以这时改用上一次存下来的读数（15 分钟内有效），宁可差几十毫秒也不能迟到。
        """
        left = deadline - time.time()
        if left > min_time:
            self.sync_clock()
            return
        cached = load_offset()
        if cached:
            self.offset, age = cached
            self.log(f"来不及重新对时（距开抢只剩 {max(0.0, left):.0f} 秒），"
                     f"沿用 {age:.0f} 秒前的读数 {self.offset:+.3f} 秒")
        else:
            self.offset = 0.0
            self.log(f"来不及对时（距开抢只剩 {max(0.0, left):.0f} 秒），"
                     f"按本机时间出手，可能偏差半秒")

    def server_now(self) -> float:
        return time.time() + self.offset

    def wait_until_server(self, target_epoch: float, spin_from: float = 0.05,
                          stop: Callable[[], bool] | None = None) -> float:
        """等待到指定服务器时间；最后 50ms 自旋，保证精度。

        stop 返回 True 时立刻放弃等待并抛 ApiError，用于「取消抢单」。
        """
        target_local = target_epoch - self.offset
        while True:
            delta = target_local - time.time()
            if delta <= spin_from:
                break
            if stop and stop():
                raise ApiError("已取消")
            time.sleep(min(delta - spin_from, 0.2))
        while time.time() < target_local:
            if stop and stop():
                raise ApiError("已取消")
            time.sleep(0.01)
        return self.server_now()

    # ---------- 场地索引 ----------
    def build_index(self, xmdm: str, xqdm: str, cgbm: str, rq: str | None = None) -> dict[str, dict]:
        """取一次场地列表，把 场地名->WID 存成永久索引（WID 跨日期固定，只需建一次）"""
        dates = []
        try:
            dates = self.c.date_list()
        except Exception:
            pass
        if rq:
            dates = [rq] + [d for d in dates if d != rq]
        if not dates:
            dates = [(dt.date.today()).strftime("%Y-%m-%d")]

        rows: list[dict] = []
        for d in dates[:3]:
            for slot in ("12:00-13:00", "19:00-20:00", "08:00-09:00"):
                ks, js = slot.split("-")
                try:
                    got = self.c.opening_rooms(xmdm=xmdm, rq=d, yylx="1.0",
                                               kssj=ks, jssj=js, xqdm=xqdm, cgbm=cgbm)
                except Exception:
                    continue
                if got:
                    rows = got
                    break
            if rows:
                break
        out: dict[str, dict] = {}
        for r in rows:
            name = core_name(r.get("CDMC"))
            if not name:
                continue
            out[name] = {
                "WID": r.get("WID", ""),
                "CGBM": r.get("CGBM", "") or cgbm,
                "XMDM": r.get("XMDM", "") or xmdm,
                "XQDM": r.get("XQDM", "") or xqdm,
                "CDMC": r.get("CDMC", ""),
                "CQBM": r.get("CQBM", ""),
            }
        if out:
            idx = load_index()
            idx[_index_key(xqdm, xmdm, cgbm)] = out
            save_index(idx)
        return out

    def get_index(self, xmdm: str, xqdm: str, cgbm: str, refresh: bool = False) -> dict[str, dict]:
        idx = load_index()
        hit = idx.get(_index_key(xqdm, xmdm, cgbm))
        if hit and not refresh:
            return hit
        self.log("正在建立场地索引（只需一次）…")
        out = self.build_index(xmdm, xqdm, cgbm)
        if out:
            self.log(f"场地索引完成：{len(out)} 片")
        return out

    # ---------- 可约性 ----------
    def check_availability(self, plan: Plan, quiet: bool = False) -> bool:
        """实时问一次服务器：这个时段现在哪几片已经被人约了、哪几片还空着。

        只读请求，不会下单。注意：**只有在场地已经放出来之后查才有意义**，
        还没放场的时候所有场地都显示「不可约」，照这个跳过就会一片都不打。
        """
        ks, js = plan.slot.split("-")
        try:
            rows = self.c.opening_rooms(xmdm=plan.xmdm, rq=plan.date, yylx=plan.yylx,
                                        kssj=ks, jssj=js, xqdm=plan.xqdm, cgbm=plan.cgbm)
        except Exception as e:
            if not quiet:
                self.log(f"查可约情况失败（不影响继续抢）：{e}")
            return False
        if not rows:
            return False
        plan.free, plan.full, plan.not_open = split_rooms(rows)
        plan.checked = True
        return True

    def wait_release(self, plan: Plan, release_at: float | None = None,
                     timeout: float | None = None, lanes: int = LANES,
                     stop: Callable[[], bool] | None = None) -> str:
        """到点开火，用几路并发只读查询盯到场地真的放出来为止。

        返回两种会真的发生的结果：
          "open"    场地放出来了，而且还有空场 —— 可以开抢
          "empty"   场地放出来了，但一片都没剩 —— 别浪费下单请求

        什么时候开始查：固定是**放场前 2 秒**（12:29:58）。
        不管你是几点点的「开始抢场」——12:00 点、12:25 点、12:29:57 点——
        点得早只是把状态标成「已就位」，中间一个请求都不发，安静等到
        12:29:58 才开第一枪。

        开火之后是几路并发，谁先看见算谁的：
        三路错开 10 毫秒发出，各自满速循环（一次往返 28 毫秒左右），
        合起来大约每 9 毫秒就有一个新请求出去。第一路看到「不再是无开放
        场地」就举手，其余两路把手头那个请求收完就停——所以真正多花的
        只有那两个已经在飞的回包，不会拖长开枪的时间。

        为什么要并发：放场那一刻全校都在刷，单次往返会从 28 毫秒涨到
        几百毫秒。单路那时变成每几百毫秒才看一眼，三路还能保持三倍。

        默认不设超时：场地没放出来就一直问，问到你点「取消等待」为止。
        这一步全是只读查询（查场地名单），不占场、不下单。
        """
        if release_at is not None:
            left = release_at - self.server_now()
            if left > FIRE_LEAD:
                at = dt.datetime.fromtimestamp(round(release_at - self.offset))
                fire = at - dt.timedelta(seconds=FIRE_LEAD)
                self.log(f"已就位。放场 {at:%H:%M:%S}，{fire:%H:%M:%S} 开始发只读查询"
                         f"（还有 {left:.0f} 秒；这中间只做一次对时，不碰抢场接口）")
                self.wait_until_server(release_at - FIRE_LEAD, stop=stop)

        lanes = max(1, int(lanes))
        ks, js = plan.slot.split("-")
        query = dict(xmdm=plan.xmdm, rq=plan.date, yylx=plan.yylx,
                     kssj=ks, jssj=js, xqdm=plan.xqdm, cgbm=plan.cgbm)

        found = threading.Event()     # 有人判定「放场了」，全场收工
        halt = threading.Event()      # 出事叫停（取消 / 连续失败）
        lock = threading.Lock()
        verdict: dict = {}
        hits = [0] * lanes
        fails = [0] * lanes
        t0 = time.time()

        def claim(kind: str, free: set[str] | None = None,
                  full: set[str] | None = None) -> None:
            """第一个拿到结论的人说了算，后来的不覆盖。"""
            with lock:
                if found.is_set():
                    return
                verdict["kind"] = kind
                if free is not None:
                    verdict["free"] = free
                    verdict["full"] = full or set()
                found.set()

        def lane(i: int) -> None:
            cli = self.c
            if i:
                # 每一路用独立连接，互不抢 socket。cookie 从主客户端拷一份，
                # 只读查询不会改动它，拷过来是安全的。
                try:
                    cli = SzuClient()
                    cli.s.cookies.update(self.c.s.cookies)
                except Exception:
                    cli = self.c
                time.sleep(i * LANE_GAP)
            while not found.is_set() and not halt.is_set():
                if stop and stop():
                    halt.set()
                    return
                try:
                    rows = cli.opening_rooms(**query)
                except Exception:
                    fails[i] += 1
                    if fails[i] >= 40:      # 连续 40 次都打不通，别硬撑
                        halt.set()
                        return
                    continue
                fails[i] = 0
                hits[i] += 1
                if not rows:
                    continue
                free, full, not_open = split_rooms(rows)
                if not not_open:
                    claim("open" if free else "empty", free, full)
                    return

        self.log(f"开始盯场，{lanes} 路并发只读查询" if lanes > 1
                 else "开始盯场，只读查询走起")
        for i in range(lanes):
            threading.Thread(target=lane, args=(i,), daemon=True,
                             name=f"watch-{i}").start()

        # 主线程只守着那个信号，20 毫秒醒一次
        while not found.is_set():
            if found.wait(0.02):
                break
            if stop and stop():
                halt.set()
                raise ApiError("已取消")
            if halt.is_set():
                # 三路都连续失败退出了，再等下去就是死循环
                claim("failed")
                break
            if timeout is not None and time.time() - t0 >= timeout:
                claim("timeout")
                break
        halt.set()

        kind = verdict.get("kind")
        spent = time.time() - t0
        total = sum(hits)
        detail = "｜各路 " + "/".join(str(h) for h in hits) if lanes > 1 else ""
        if kind == "failed":
            self.log("三路只读查询连续失败，可能登录态失效或网络断了，没有下单")
            raise ApiError("只读查询连续失败，检查一下登录态和网络")
        if kind == "open":
            plan.free = verdict["free"]
            plan.full = verdict["full"]
            plan.not_open = False
            plan.checked = True
            self.log(f"只读确认已放场（盯了 {spent:.2f} 秒，共查 {total} 次{detail}），"
                     f"可约 {len(plan.free)} 片")
            return "open"
        if kind == "empty":
            plan.free = set()
            plan.full = verdict.get("full") or set()
            plan.not_open = False
            plan.checked = True
            self.log(f"只读确认已放场，但一片空场都没有（共查 {total} 次{detail}）")
            return "empty"
        self.log(f"只读盯了 {spent:.0f} 秒，{plan.date} {plan.slot} 还是没放场，"
                 f"共查 {total} 次{detail}，没有下单")
        return "timeout"

    # ---------- 预热 ----------
    def prepare(self, date: str, slot: str, courts: list[str],
                xmdm: str = "001", xqdm: str = "1", cgbm: str = "",
                yylx: str = "1.0", refresh: bool = False,
                check_live: bool = False, verify: bool = True,
                pending: bool = False) -> Plan:
        plan = Plan(date=date, slot=slot, xmdm=xmdm, xqdm=xqdm, cgbm=cgbm,
                    yylx=yylx, pending=pending)
        # 清单里多条场地共用一个登录态，只有第一条值得再验一次
        if verify and not self.c.is_logged_in():
            raise ApiError("登录状态已失效，请先重新登录")

        rows = self.get_index(xmdm, xqdm, cgbm, refresh=refresh)
        if not rows:
            # 索引建不出来时退回实时查询（多花一次往返，但不至于开抢时没数据）
            ks, js = slot.split("-")
            got = self.c.opening_rooms(xmdm=xmdm, rq=date, yylx=yylx,
                                       kssj=ks, jssj=js, xqdm=xqdm, cgbm=cgbm)
            rows = {core_name(r.get("CDMC")): {
                "WID": r.get("WID", ""), "CGBM": r.get("CGBM", "") or cgbm,
                "XMDM": r.get("XMDM", "") or xmdm, "XQDM": r.get("XQDM", "") or xqdm,
                "CDMC": r.get("CDMC", ""), "CQBM": r.get("CQBM", ""),
            } for r in got}

        plan.rows = rows
        # 界面传过来的是「羽毛球场B4」，索引里存的是「B4」，统一成核心名再比
        want = [core_name(c) for c in courts]
        plan.courts = [c for c in want if c in rows]
        missing = [c for c in want if c not in rows]

        # 清单里写了不存在的场地时（B 区根本没有 B1/B2/B3），从同区还没被选中的
        # 场地里按名字顺序补一个。否则「勾了 4 片、实际只打 3 片」会悄悄发生，
        # 而你以为 4 片都试过了。
        filled: list[tuple[str, str]] = []
        still: list[str] = []
        if missing:
            pool = sorted(c for c in rows if c not in plan.courts)
            for m in missing:
                zone = (m or " ")[0]
                cand = next((c for c in pool if c.startswith(zone)), None)
                if not cand:
                    still.append(m)
                    continue
                pool.remove(cand)
                plan.courts.append(cand)
                filled.append((m, cand))

        self.log(f"预热完成：可用场地 {len(plan.courts)} 片，优先 {plan.courts[0] if plan.courts else '无'}")
        for m, cand in filled:
            self.log(f"（清单里的 {m} 不存在，用同区的 {cand} 顶上）")
        if still:
            self.log(f"（这些场地不存在，同区也没有能顶的：{', '.join(still)}）")

        # 场地已经放出来了（不是 12:30 那一枪）才有必要查：现在谁被约了。
        # 12:30 之前查是没意义的——那时整批都显示「不可约」，照此跳过会一片都不打。
        if check_live and plan.courts and self.check_availability(plan):
            skip = [c for c in plan.courts if c in plan.full]
            keep = plan.order()
            if skip:
                self.log(f"已经被人约了，直接跳过不浪费请求：{'、'.join(skip)}")
            if not keep:
                self.log("这个时段现在一片空场都没有，提交也只会被判「已满」")
            else:
                self.log(f"实际会依次尝试：{' → '.join(keep)}")
        return plan

    # ---------- 开抢 ----------
    def build(self, plan: Plan, court: str) -> dict | None:
        row = plan.rows.get(court)
        if not row or not row.get("WID"):
            return None
        room = {
            "WID": row["WID"],
            "CGBM": row.get("CGBM") or plan.cgbm,
            "XMDM": row.get("XMDM") or plan.xmdm,
            "XQDM": row.get("XQDM") or plan.xqdm,
        }
        if row.get("CQBM"):
            room["CQBM"] = row["CQBM"]
        return build_form(room, plan.date, plan.slot, plan.yylx, self.user_id, self.user_name)

    def fire_once(self, plan: Plan, court: str, form: dict | None = None) -> dict:
        form = form or self.build(plan, court)
        if not form:
            return {"code": "LOCAL", "msg": f"场地 {court} 没有 WID"}
        t0 = time.perf_counter()
        try:
            if self.on_fire:
                self.on_fire()
            resp = self.c.insert_booking(form)
        except Exception as e:
            cost = (time.perf_counter() - t0) * 1000
            text = str(e)
            # 超时是最纠结的一种失败：服务器很可能已经收下了，只是回包晚了。
            # 客户端既没法撤销、也没法确认，更不能自动重试（真重试就可能订到两片）。
            # 所以这里必须说人话——别让人当成「没抢到」再抢一次，最后订到两片
            # 还得跑去场馆取消。
            low = text.lower()
            if "timed out" in low or "timeout" in low:
                msg = ("提交超时：服务器可能已经收到这笔预约。"
                       "请先去「我的预约」确认，确认没有再说重抢")
                self.log(f"提交 {court} 超时（{cost:.0f}ms）：{msg}")
                return {"code": "TIMEOUT", "court": court, "msg": msg,
                        "uncertain": True}
            self.log(f"提交 {court} 出错（{cost:.0f}ms）：{text}")
            return {"code": "EXC", "court": court, "msg": text}
        cost = (time.perf_counter() - t0) * 1000
        msg = resp.get("msg") or ("成功" if str(resp.get("code")) == "0" else "")
        self.log(f"提交 {court} → {resp.get('code')} {msg}（{cost:.0f}ms）")
        return resp

    def run(self, plan: Plan, at_epoch: float, max_tries: int = 4,
            gap_ms: int = 180, retry_early: bool = True,
            stop: Callable[[], bool] | None = None,
            early_rounds: int = 4, early_gap: float = 0.15,
            lanes: int = LANES) -> dict:
        """等到开抢时刻，按优先级依次提交；被抢走就换下一片，遇限流立即停。

        关于「会不会对已经被抢走的场地重复发请求」：
        - 已经被别人约掉的（实时查出来的）→ 一开始就不打，直接跳过。
        - 「已满 / 已被预约」→ 记一笔日志，立刻换下一片，**不再**打它。
        - 「未开始 / 尚未开放」→ 这不是被别人抢走，是服务器还没放场，
          这时会对同一片补打（最多 3 轮），因为整批场地共用一个放场开关。
        """
        # 目标日期是「今天 12:30 才放场」的情况：不用下单接口去撞，
        # 拿只读查询盯住，确认放出来了再打。撞「未开放」虽然无害，
        # 但白占提交次数，看着也心慌。
        if plan.pending:
            # 注意这里**不先 wait_until_server**：点下去就开始盯。
            # 原来的写法是先睡到 12:30:00 再开始查，等于把 12:29:5x 点下去
            # 之后那几秒全用在睡觉上；万一学校那边早半秒放场，我们还在睡。
            # 只读查询早发没有任何副作用，看见就开枪，比卡秒表稳。
            state = self.wait_release(plan, release_at=at_epoch,
                                      lanes=lanes, stop=stop)
            if state == "timeout":
                return {"code": "EARLY", "court": "",
                        "msg": "一直盯到超时也没看到放场，这次没有提交任何订单"}
            if state == "empty":
                return {"code": "FULL", "court": "",
                        "msg": "场地放出来了，但已经被抢光，没有可提交的场地"}
        else:
            self.wait_until_server(at_epoch, stop=stop)

        cands = plan.order()
        if plan.checked and not cands:
            # 全都被约掉了，再发请求只会换回「已满」，白白增加被判定操作频繁的风险
            self.log("这个时段所有场地都已被约，不再发起提交")
            return {"code": "FULL", "msg": f"{plan.slot} 已满场，没有可提交的场地",
                    "court": "", "no_submit": True}

        tries = cands[:max(1, max_tries)]
        last: dict = {"code": "NONE", "msg": "没有可尝试的场地"}
        cur = tries[0] if tries else ""

        # 预先把表单都拼好，省掉开抢时的字符串处理时间
        forms = {c: self.build(plan, c) for c in tries}
        rounds = max(1, early_rounds) if retry_early else 1

        for rnd in range(rounds):
            not_open = False          # 这一轮是不是「压根还没放场」
            for i, court in enumerate(tries):
                cur = court
                if stop and stop():
                    self.log("已取消，停止提交")
                    return {**last, "court": cur, "cancelled": True}
                if i:
                    time.sleep(gap_ms / 1000)
                resp = self.fire_once(plan, court, forms.get(court))
                last = resp
                code = str(resp.get("code"))
                msg = str(resp.get("msg") or "")
                if code == "0":
                    return {**resp, "court": court}
                if code == "TIMEOUT":
                    # 结果不确定：可能已经订上了。这时候绝不能换下一片接着打，
                    # 那会变成「一人占两片」，用户还得自己去取消一笔。
                    return {**resp, "court": court}
                if any(w in msg for w in STOP_WORDS):
                    return {**resp, "court": court}
                if any(w in msg for w in EARLY_WORDS):
                    # 还没放场：整批场地共用一个开关，一片没开就全没开，
                    # 不要挨个把 4 片都打一遍浪费请求
                    not_open = True
                    break
                if any(w in msg for w in FULL_WORDS):
                    self.log(f"{court} 已被占，换下一片")
                    continue
                # 不认识的报错就先停，避免瞎打把账号打限流
                return {**resp, "court": court}

            if not not_open:
                break                 # 整批都试完了（都被占），收工
            if rnd + 1 >= rounds:
                break                 # 没放场，但也没有补打机会了
            self.log(f"场地还没放出来，{int(early_gap * 1000)} 毫秒后再试一轮")
            time.sleep(early_gap)

        return {**last, "court": cur}


def next_release_date(now: dt.datetime | None = None) -> str:
    """开抢时预约的是明天"""
    now = now or dt.datetime.now()
    return (now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
