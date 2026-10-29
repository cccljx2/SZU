# -*- coding: utf-8 -*-
"""本地服务：给界面提供接口，同时承载抢单引擎"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import api
from api import SzuClient
from engine import BookingEngine, core_name, load_clock, FIRE_LEAD

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class State:
    def __init__(self):
        self.logs: deque = deque(maxlen=400)
        self.log_seq = 0          # 一共打过多少条日志（只增不减）
        self.status = "待命"
        self.busy = False
        self.user: dict = {}
        self.client: SzuClient | None = None
        self.catalog: dict = {}
        self.last_result: dict = {}
        self.submitted = 0        # 本次运行内真实提交次数
        self.throttled = False    # 是否已触发限流
        self.last_seen = 0.0      # 界面最后一次心跳
        self.cancel_requested = False   # 用户点了「取消等待」
        self.firing = False       # 是否已经进入「真的在提交」阶段
        self.clock: dict = {}     # 最近一次对时的结果，用于界面显示

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.logs.append(line)
        # logs 是个 400 条的环形队列，满了之后长度就恒等于 400。
        # 界面如果按「长度变了没有」决定要不要重画，就会在满 400 条那一刻
        # 永久卡住，后面抢到没抢到全看不到。所以另外记一个只增不减的序号。
        self.log_seq += 1
        try:
            with (ROOT / "capture" / "app.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


S = State()

# 目录只在第一次真正去拉，避免界面轮询时并发拉好几份
_CAT_LOCK = threading.Lock()


def boot():
    try:
        import accounts
        msg = accounts.migrate_legacy()
        if msg:
            S.log(msg)
    except Exception as e:
        S.log(f"账号目录初始化失败：{e}")
    S.user = _load_user()
    # 启动时对齐一次：user.json 里的学号、current.txt、配置目录名三者
    # 会因为「用 A 的配置登录了 B 的号」而对不上，界面就会自相矛盾。
    try:
        import accounts as _acc
        u0 = _acc.current_uid()
        u1 = _acc.reconcile()
        if u0 and u1 and u0 != u1:
            S.log(f"账号目录已按实际登录的 {u1} 对齐（原 {u0}）")
    except Exception as e:
        S.log(f"账号对齐失败（不影响使用）：{e}")
    if os.environ.get("SZU_OFFLINE_TEST"):
        cached = ROOT / "capture" / "venue_data.json"
        if cached.exists():
            S.catalog = json.loads(cached.read_text(encoding="utf-8"))
        try:
            S.client = SzuClient(api.COOKIE_FILE)   # 只读 cookie，不发请求
        except Exception:
            pass
        S.log("离线测试模式：使用本地缓存目录，不访问网络")
        S.status = "待命（测试模式）"
        return
    try:
        S.client = SzuClient(api.COOKIE_FILE)
    except Exception:
        S.client = None
        S.log("尚未登录，请点「重新登录」")
        S.status = "未登录"
        return
    try:
        if S.client.is_logged_in():
            S.catalog = S.client.venue_data()
            S.log("登录态有效，场馆目录已加载")
            S.status = "待命"
        else:
            S.log("登录态已失效，请点「重新登录」")
            S.status = "未登录"
    except Exception as e:
        S.log(f"初始化失败：{e}")
        S.status = "初始化失败"


def _load_user() -> dict:
    f = ROOT / "capture" / "user.json"
    if f.exists():
        try:
            u = json.loads(f.read_text(encoding="utf-8"))
            if not u.get("name"):
                u["name"] = (u.get("USER_INFO") or {}).get("name") or ""
            return u
        except Exception:
            pass
    return {}


RELEASE_HH = 12
RELEASE_MM = 30


def release_epoch(date_str: str) -> float | None:
    """某一天的场地，实际是在**前一天 12:30** 放出来的。

    这是整件事的关键：要抢 10-30 的场，开抢时刻是 10-29 的 12:30，
    不是 10-30 的 12:30。日期和放场时刻必须按这个关系换算。
    """
    try:
        d = dt.datetime.strptime(str(date_str).strip()[:10], "%Y-%m-%d")
    except Exception:
        return None
    r = (d - dt.timedelta(days=1)).replace(hour=RELEASE_HH, minute=RELEASE_MM,
                                           second=0, microsecond=0)
    return r.timestamp()


def next_release_epoch() -> float:
    """下一次 12:30（今天还没到就是今天，已经过了就是明天）"""
    now = dt.datetime.now()
    t = now.replace(hour=RELEASE_HH, minute=RELEASE_MM, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t.timestamp()


def target_epoch() -> float:
    return next_release_epoch()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/api/status":
            S.last_seen = time.time()
            # 界面把当前选中的日期带上来，倒计时才能按「前一天 12:30」算
            q = urllib.parse.parse_qs(u.query or "")
            want = (q.get("date") or [""])[0]
            at = release_epoch(want) if want else None
            nxt = next_release_epoch()
            nxt_date = (dt.datetime.fromtimestamp(nxt) + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            ck = dict(S.clock)
            if ck.get("at"):
                ck["age"] = round(time.time() - ck["at"], 1)
            return self._json({
                "status": S.status,
                "busy": S.busy,
                "user": S.user,
                "logged_in": bool(S.user.get("USERID")) and S.client is not None,
                "logs": list(S.logs),
                "log_seq": S.log_seq,
                "target": at if at is not None else nxt,
                "next": nxt,
                "next_date": nxt_date,
                "released": bool(at is not None and at <= time.time()),
                "now": time.time(),
                "result": S.last_result,
                "submitted": S.submitted,
                "throttled": S.throttled,
                "firing": S.firing,
                "cancel_requested": S.cancel_requested,
                "clock": ck,
            })
        if path == "/api/catalog":
            try:
                return self._json(self._catalog())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/accounts":
            import accounts
            u = accounts.read_user()
            return self._json({"saved": accounts.list_saved(),
                               "current": accounts.current_uid(),
                               "current_name": u.get("name", "")})
        if path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        data = self._body()
        try:
            if path == "/api/courts":
                return self._json(self._courts(data))
            if path == "/api/slots":
                return self._json(self._slots(data))
            if path == "/api/dates":
                return self._json(self._dates(data))
            if path == "/api/start":
                return self._json(self._start(data))
            if path == "/api/cancel":
                if not S.busy:
                    return self._json({"ok": False, "error": "当前没有正在等待的任务"})
                S.cancel_requested = True
                S.log("收到取消请求，正在停止…")
                return self._json({"ok": True})
            if path == "/api/reset":
                if S.busy:
                    return self._json({"ok": False, "error": "正在执行中，等它跑完再刷新"})
                S.submitted = 0
                S.throttled = False
                S.last_result = {}
                S.cancel_requested = False
                S.firing = False
                S.status = "待命"
                S.log("已刷新：提交次数归零，可以重新开始")
                return self._json({"ok": True})
            if path == "/api/login":
                threading.Thread(target=self._login, daemon=True).start()
                return self._json({"ok": True})
            if path == "/api/switch":
                if S.busy:
                    return self._json({"ok": False, "error": "正在执行中"})
                S.busy = True          # 先占住，避免连点两次起两个线程
                threading.Thread(target=_do_switch, args=(data,), daemon=True).start()
                return self._json({"ok": True})
            if path == "/api/bye":
                # 历史遗留接口，保留但不再退出进程。
                # 浏览器在关闭/恢复标签页时会触发 unload，曾导致主程序被误杀。
                return self._json({"ok": True})
            if path == "/api/quit":
                self._json({"ok": True, "busy": S.busy})
                if not S.busy:
                    S.log("用户主动退出程序")
                    threading.Timer(0.5, lambda: os._exit(0)).start()
                return None
        except api.ApiError as e:
            # 业务层错误（时段没放、场地已满、参数不全）走正常返回，
            # 别用 500，否则界面上会显示成「后台出错」
            return self._json({"ok": False, "error": str(e)})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---------- 业务 ----------
    def _catalog(self):
        # 登录刚完成时目录可能还没拉下来（登录线程还在跑），这里补一次，
        # 免得界面一直停在「登录后加载」。
        if not S.catalog and S.client:
            with _CAT_LOCK:
                if not S.catalog:
                    try:
                        S.catalog = S.client.venue_data()
                        S.log("场馆目录已加载")
                    except Exception as e:
                        raise RuntimeError(f"目录加载失败：{e}")
        if not S.catalog:
            raise RuntimeError("目录未加载")
        camps = S.catalog.get("campusList", [])
        xm = S.catalog.get("xmList", [])
        pk = S.catalog.get("packageVenueList", [])
        return {"ok": True, "campus": camps, "sports": xm, "venues": pk}

    def _template_rows(self, xmdm, xqdm, cgbm, ks, js, date):
        """目标日期还没放场时，往前找一天已经放场的，借它的场地名单。

        场地名字每天固定（B4 永远叫 B4），变的只是「哪几片被占了」。
        所以拿前一天的名单来排优先级完全够用，12:30 放场后照这个顺序打，
        哪片满了就换下一片。
        """
        try:
            base = dt.date.fromisoformat(str(date)[:10])
        except Exception:
            return [], ""
        for back in range(1, 4):
            d = (base - dt.timedelta(days=back)).strftime("%Y-%m-%d")
            try:
                rows = S.client.opening_rooms(xmdm=xmdm, rq=d, yylx="1.0",
                                              kssj=ks, jssj=js, xqdm=xqdm, cgbm=cgbm)
            except Exception:
                continue
            if rows:
                return rows, d
        return [], ""

    def _courts(self, d):
        if not S.client:
            return {"ok": False, "error": "还没登录，请先点右上角「重新登录」"}
        venue_cgbm = d.get("cgbm")
        xmdm = d.get("xmdm")
        date = d.get("date")
        slot = d.get("slot")
        # 目录还没加载完时这几个是空的，直接去问服务器只会报「缺少必要参数」
        if not (venue_cgbm and xmdm and date and slot):
            return {"ok": False, "error": "场馆还没加载好，先在上面选好校区、项目、场馆"}
        if "-" not in str(slot):
            return {"ok": False, "error": f"时段格式不对：{slot}"}
        ks, js = slot.split("-")
        try:
            rows = S.client.opening_rooms(xmdm=xmdm, rq=date, yylx="1.0",
                                          kssj=ks, jssj=js, xqdm=d.get("xqdm"),
                                          cgbm=venue_cgbm)
        except api.ApiError as e:
            # 还没到放场时间（"该预约日期暂未开放预约"）。
            # 这不是错误，是这个日期客观上还查不到——给一份模板名单，
            # 让用户现在就能把优先级排好，12:30 到点自动照单开枪。
            if "未开放" not in str(e) and "暂未" not in str(e):
                raise
            rows, tpl = self._template_rows(xmdm, d.get("xqdm"), venue_cgbm, ks, js, date)
            if not rows:
                return {"ok": False,
                        "error": f"{date} 的场地还没放出来，也找不到可以参照的名单"}
            out = [{"name": r["CDMC"], "free": False, "why": "未放场"} for r in rows]
            out.sort(key=lambda x: x["name"])
            S.log(f"{date} 还没放场，借用 {tpl} 的名单排优先级：共 {len(out)} 片")
            return {"ok": True, "courts": out, "template": True, "template_date": tpl}

        out = [{"name": r["CDMC"],
                "free": not r["disabled"],
                "why": r.get("text") or ("已满" if r["disabled"] else "可约")} for r in rows]
        out.sort(key=lambda x: x["name"])
        S.log(f"场地：共 {len(out)} 片，可约 {sum(1 for x in out if x['free'])} 片")
        return {"ok": True, "courts": out}

    def _slots(self, d):
        if not S.client:
            return {"ok": False, "error": "还没登录，请先点右上角「重新登录」"}
        if not d.get("date"):
            return {"ok": False, "error": "还没选日期"}
        rows = S.client.time_list(xq=d.get("xqdm") or "1", rq=d["date"],
                                  yylx="1.0", xmdm=d.get("xmdm") or "001")
        out = [{"slot": t.get("CODE"),
                "free": not t.get("disabled"),
                "why": t.get("text") or ("已满" if t.get("disabled") else "可预约")}
               for t in rows if t.get("CODE")]
        return {"ok": True, "slots": out}

    def _dates(self, d):
        if not S.client:
            return {"ok": False, "error": "还没登录，请先点右上角「重新登录」"}
        return {"ok": True, "dates": S.client.date_list()}

    def _start(self, d):
        if S.busy:
            return {"ok": False, "error": "正在执行中"}
        if not S.client:
            return {"ok": False, "error": "未连接"}
        if not S.user.get("USERID"):
            return {"ok": False, "error": "还没有登录账号，请先点右上角「重新登录」"}
        # 已经提交过还想再来一次：界面会先让用户确认，确认后带 force=True
        if not d.get("dry") and S.submitted >= 1 and not d.get("force"):
            return {"ok": False,
                    "error": f"本次运行已经提交过 {S.submitted} 次。\n"
                             "如果确实要再抢一次，请点「刷新」后再开始，或再点一次并确认。"}
        S.busy = True
        S.cancel_requested = False
        S.firing = False
        S.last_result = {}
        threading.Thread(target=_run_booking, args=(d,), daemon=True).start()
        return {"ok": True}

    def _login(self):
        import accounts
        prof = accounts.active_profile()
        S.log(f"请在弹出的浏览器窗口中登录（配置目录 {prof.name}）")
        info = _spawn_login(prof)
        if not info:
            S.user = {}
            S.status = "登录未完成"
            return
        try:
            # 让配置目录名、current.txt 和实际登录的账号对齐。
            # 用 A 的配置登录了 B 的学号时，这里会把目录改名成 B，
            # 否则界面会出现「已登录 B，下拉框却显示 A（当前）」这种自相矛盾。
            uid = str(info.get("USERID") or "")
            if uid:
                accounts.align(uid, info.get("name", ""))
            client = SzuClient(api.COOKIE_FILE)
            # 先把目录拿到手，再一次性切换状态。
            # 否则界面会在这条线上看到「已登录、但目录是空的」，
            # 下拉框就停在「登录后加载」不动了。
            try:
                catalog = client.venue_data()
            except Exception as e:
                catalog = {}
                S.log(f"登录成功，但场馆目录没拉下来：{e}")
            S.client = client
            S.catalog = catalog
            S.user = info
            S.submitted = 0
            S.throttled = False
            S.last_result = {}
            S.status = "待命" if catalog else "目录加载失败"
            S.log(f"当前账号：{info.get('name', '')} {info.get('USERID', '')}")
        except Exception as e:
            S.user = info
            S.log(f"登录成功但初始化失败：{e}")
            S.status = f"加载失败：{e}"


def _spawn_login(profile=None, timeout: int = 300) -> dict | None:
    """在独立进程里打开登录窗口，Playwright 崩了也不会带走主程序"""
    import accounts
    exe = ROOT / ".venv" / "Scripts" / "python.exe"
    script = ROOT / "src" / "login_window.py"
    if not exe.exists():
        exe = pathlib.Path(sys.executable)
    profile = pathlib.Path(profile) if profile else accounts.active_profile()
    try:
        (ROOT / "capture").mkdir(parents=True, exist_ok=True)
        log = open(ROOT / "capture" / "login.log", "a", encoding="utf-8")
    except Exception:
        log = subprocess.DEVNULL
    try:
        p = subprocess.Popen([str(exe), str(script), str(profile)], cwd=str(ROOT),
                             stdout=log, stderr=log,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        p.wait(timeout=timeout)
    except Exception as e:
        S.log(f"登录进程启动失败：{e}")
        return None
    finally:
        try:
            if hasattr(log, "close"):
                log.close()
        except Exception:
            pass
    u = accounts.read_user()
    return u if u.get("USERID") else None


def _do_switch(d: dict):
    try:
        import accounts
        from login import refresh_session
    except Exception as e:
        S.log(f"切换账号失败（模块导入出错）：{e!r}")
        S.status = f"切换失败：{e}"
        return
    try:
        S.busy = True
        S.status = "正在切换账号…"
        uid = d.get("uid")
        if uid:
            prof = accounts.profile_of(uid)
            if not prof.exists():
                S.status = f"找不到账号 {uid} 的登录态"
                S.log(f"账号 {uid} 没有保存过登录态")
                return
            accounts.set_current(uid)
            S.log(f"正在恢复账号 {uid} 的登录态…")
            info = refresh_session(prof)
            if not info:
                S.user = {}
                S.client = None
                S.status = "该账号登录态已失效，请点「重新登录」"
                S.log("恢复失败：登录态过期，需要重新登录")
                return
        else:
            S.log("准备登录新账号：清空当前凭证")
            accounts.set_current("")
            accounts.clear_credentials()
            S.user = {}
            S.client = None
            S.log("请在弹出的浏览器窗口中登录新账号")
            info = _spawn_login(accounts.SCRATCH)
            S.log(f"登录窗口已关闭，结果：{info or '未取到身份'}")
            if not info:
                S.status = "登录未完成"
                return
            accounts.adopt_scratch(info.get("USERID", ""), info.get("name", ""))
            S.log(f"已把新账号保存为 {info.get('USERID')}")
        client = SzuClient(api.COOKIE_FILE)
        try:
            catalog = client.venue_data()
        except Exception as e:
            catalog = {}
            S.log(f"账号已切换，但场馆目录没拉下来：{e}")
        S.client = client
        S.catalog = catalog
        S.user = info
        S.submitted = 0
        S.throttled = False
        S.last_result = {}
        S.status = "待命" if catalog else "目录加载失败"
        S.log(f"当前账号：{info.get('name', '')} {info.get('USERID', '')}")
    except Exception as e:
        S.log(f"切换账号失败：{e}")
        S.status = f"切换失败：{e}"
    finally:
        S.busy = False


_COUNT_LOCK = threading.Lock()


def _bump_submit():
    with _COUNT_LOCK:
        S.submitted += 1
        S.firing = True      # 只有真的发出去了才算「已开始提交」


def _fire_parallel(plans, at: float, tries: int, gap: int, base) -> dict[str, dict]:
    """每个场次一个线程，同一时刻一起出手。

    串行的话第二个场次会晚一秒多，12:30 那一秒里晚一秒基本等于没抢。
    每个线程用自己的一条连接，互不干扰；任一场次被限流就喊停其他场次。

    但「盯放场」这件事不能每个线程各干各的：整批场地共用一个放场开关，
    两路线程各刷一遍就是双倍请求量。所以在开火之前先由主引擎盯一次，
    确认放出来了，再让所有线程同时出手。
    """
    out: dict[str, dict] = {}
    stop = threading.Event()

    pend = [p for p in plans if p.pending]
    fire_at = at          # 默认按原定时刻开火
    if pend:
        st = base.wait_release(pend[0], release_at=at,
                               stop=lambda: S.cancel_requested or stop.is_set())
        if st == "timeout":
            return {p.slot: {"code": "EARLY", "court": "",
                             "msg": "一直盯到超时也没看到放场，没有提交任何订单"}
                    for p in plans}
        # 盯场已经确认放出来了，就别再等到整点。
        # 学校偶尔会比 12:30:00 早一两秒放场，那几秒正是最值钱的；
        # 子引擎看到 pending=False 会先去 wait_until_server(at)，等到整点才动手，
        # 等于把捡到的便宜又原样丢掉。这里把时刻改到「现在」，看见就打。
        fire_at = min(at, time.time())
        # 放场了。每个场次再各查一次（各 1 次请求），拿到自己那个时段的空场名单。
        # 第一个场次的名单刚才盯场时已经顺手查到了，不用重复。
        for p in plans:
            p.pending = False
            if p is not pend[0] and not p.checked:
                base.check_availability(p, quiet=True)

    def one(p):
        try:
            cli = SzuClient(api.COOKIE_FILE)
        except Exception:
            cli = S.client
        e = BookingEngine(cli, S.log, on_fire=_bump_submit)
        e.user_id = base.user_id
        e.user_name = base.user_name
        e.offset = base.offset           # 用同一个对时结果，别各自算
        e.clock_err = base.clock_err
        return e.run(p, fire_at, max_tries=tries, gap_ms=gap,
                     stop=lambda: S.cancel_requested or stop.is_set())

    with ThreadPoolExecutor(max_workers=max(1, len(plans))) as pool:
        futs = [(p, pool.submit(one, p)) for p in plans]
        for p, f in futs:
            try:
                out[p.slot] = f.result()
            except Exception as e:
                out[p.slot] = {"code": "EXC", "msg": str(e)}
            if any(w in str((out[p.slot] or {}).get("msg") or "")
                   for w in ("频繁", "稍后再试", "操作过快")):
                stop.set()
    return out


def _make_engine() -> BookingEngine:
    e = BookingEngine(S.client, S.log, on_fire=_bump_submit)
    e.user_id = S.user.get("USERID", "")
    e.user_name = S.user.get("userName") or S.user.get("name") or ""
    return e


def _rehearse(client, date: str, slot: str, plan, tries: int) -> dict:
    """演练：只读查询，一次提交都不发。

    会真的去问服务器「这个时段开了吗、还剩哪几片」，
    所以演练结果是有信息量的，不是空跑。
    """
    ks, js = slot.split("-")
    try:
        tl = client.time_list(xq=plan.xqdm or "1", rq=date, yylx="1.0",
                              xmdm=plan.xmdm or "001")
    except Exception as e:
        tl = []
        S.log(f"演练：时段查询失败 {e}")
    me = next((t for t in tl if str(t.get("CODE")) == slot), None)
    if me is not None and me.get("disabled"):
        why = me.get("text") or "不可约"
        S.log(f"演练结果：{date} {slot} 目前是满的（{why}）——现在提交也只会被判「已满」")
        return {"ok": False, "dry": True, "court": plan.courts[0] if plan.courts else "",
                "note": f"该时段目前满场（{why}）"}

    if plan.checked:
        free = sorted(plan.free)
    else:
        try:
            rows = client.opening_rooms(xmdm=plan.xmdm, rq=date, yylx="1.0",
                                        kssj=ks, jssj=js, xqdm=plan.xqdm, cgbm=plan.cgbm)
        except Exception as e:
            rows = []
            S.log(f"演练：场地查询失败 {e}")
        free = [core_name(r.get("CDMC")) for r in rows if not r.get("disabled")]

    will_try = plan.order()[:tries]        # 已经跳过被约掉的那些
    skipped = [c for c in plan.courts[:tries] if c in plan.full]
    if free:
        S.log(f"演练结果：{date} {slot} 可约 {len(free)} 片（{', '.join(free)}）")
        msg = f"若现在开抢，会依次打 {' → '.join(will_try)}"
        if skipped:
            msg += f"；{('、'.join(skipped))} 已经被人约了，会直接跳过"
        S.log(f"演练结果：{msg}")
        return {"ok": True, "dry": True, "court": will_try[0] if will_try else "",
                "note": f"可约 {len(free)} 片；跳过已满的 {len(skipped)} 片，"
                        f"实际依次打 {len(will_try)} 片"}
    S.log(f"演练结果：{date} {slot} 一片都没空，现在开抢大概率抢不到")
    return {"ok": False, "dry": True, "court": plan.courts[0] if plan.courts else "",
            "note": "该时段已无空场"}


def _normalize_items(d: dict) -> list[dict]:
    """把「清单」和老的「单条」两种入参统一成清单格式。

    同一时段只订一片：重复的时段只保留第一条，避免同一时间占两片场地。
    """
    raw = d.get("items")
    if not raw:
        raw = [{"slot": d.get("slot"), "courts": d.get("courts") or []}]
    out: list[dict] = []
    seen: set[str] = set()
    for it in raw:
        slot = str(it.get("slot") or "").strip()
        cs = [c for c in (it.get("courts") or []) if c]
        if not slot or not cs or slot in seen:
            continue
        seen.add(slot)
        out.append({"slot": slot, "courts": cs})
    return out


def _run_booking(d: dict):
    try:
        items = _normalize_items(d)
        if not items:
            raise RuntimeError("没有选择任何场地")
        date = d["date"]
        xmdm = d["xmdm"]
        xqdm = d["xqdm"]
        cgbm = d.get("cgbm") or ""
        dry = bool(d.get("dry"))
        tries = max(1, min(8, int(d.get("tries") or 4)))
        gap = max(50, min(2000, int(d.get("gap") or 180)))

        today = dt.date.today().strftime("%Y-%m-%d")
        if date < today:
            raise RuntimeError(f"目标日期 {date} 已经过去了（页面可能开了一整天没刷新），请重新选择")

        # 开抢时刻 = 目标日期的前一天 12:30
        # 要抢明天（10-30）的场，开抢是**今天** 12:30，不是明天 12:30。
        at = release_epoch(date)
        if at is None:
            raise RuntimeError(f"日期格式不对：{date}")
        released = at <= time.time()
        if d.get("now"):
            at = time.time() - 0.05
            released = True

        eng = _make_engine()

        # 开抢前把登录态续一次。程序可能从昨晚一直开到现在，浏览器会话 cookie
        # （MOD_AUTH_CAS 这类没有过期时间的会话 cookie）早就被服务端清掉了。
        # 用保存的浏览器配置重新走一遍 CAS（里面还有 14 天有效的 CASTGC），
        # 拿一份新 cookie，比等到 12:30 才发现登录失效强。
        if not d.get("now") and not dry and at - time.time() > 60:
            S.status = "校验登录态"
            S.log("开抢前先检查登录态是否还有效…")
            try:
                import accounts as _acc
                import login as _login
                prof = _acc.active_profile()
                if prof.exists():
                    _info = _login.refresh_session(prof)
                    if _info:
                        S.client = SzuClient(api.COOKIE_FILE)
                        S.user = _info
                        eng = _make_engine()
                        S.log("登录态有效，已刷新凭证")
                    else:
                        S.log("警告：登录态可能已经过期，若开抢失败请点「重新登录」")
            except Exception as e:
                S.log(f"登录态检查失败（不影响继续尝试）：{e}")

        if released:
            # 立即提交用不上时差：时差只影响「12:30 整点那一枪」的精度。
            # 这里如果还照走 4 秒的对时，等于白让 4 秒。
            S.status = "预热中"
            S.log("场地已放出，直接提交（立即模式不需要对时，省掉约 4 秒）")
            ck = load_clock()       # 顺手把上次的对时结果显示出来，让人心里有数
            if ck:
                S.clock = {"offset": round(float(ck["offset"]), 3),
                           "err": round(float(ck.get("err", 0.5)), 3),
                           "at": float(ck["at"])}
        else:
            S.status = "对时中"
            eng.ensure_clock(at)
            S.clock = {"offset": round(eng.offset, 3), "err": round(eng.clock_err, 3),
                       "at": time.time()}
        # 清单里每个场次都先预热好，开抢那一刻就不用再算东西了
        S.status = "预热中"
        plans = []
        for i, it in enumerate(items):
            p = eng.prepare(date, it["slot"], it["courts"], xmdm=xmdm, xqdm=xqdm,
                            cgbm=cgbm, check_live=released, verify=(i == 0),
                            pending=not released)
            if p.courts:
                plans.append(p)
            else:
                S.log(f"{it['slot']}：没有可选场地，跳过")
        if not plans:
            raise RuntimeError("清单里没有可用的场地")

        left = at - time.time()
        if released:
            S.log(f"{date} 的场地已经开放，共 {len(plans)} 个场次，直接提交")
        elif left > 3:
            S.log(f"{date} 的场地将在 {dt.datetime.fromtimestamp(at):%m-%d %H:%M:%S} 放出"
                  f"（还有 {left / 3600:.2f} 小时），共 {len(plans)} 个场次")

        # ---------- 演练 ----------
        if dry:
            if left > 120:
                S.log("演练模式：先做一次可约性检查")
            elif left > 0:
                S.status = "演练待命"
                S.log("演练模式：等到放场那一刻再看结果，期间可随时点「取消等待」")
                eng.wait_until_server(at, stop=lambda: S.cancel_requested)
            results = []
            for p in plans:
                r = _rehearse(S.client, date, p.slot, p, tries)
                r["slot"] = p.slot
                results.append(r)
            S.log("演练模式：以上全部是只读查询，没有提交任何订单")
            ok_n = sum(1 for r in results if r.get("ok"))
            S.status = "演练完成"
            S.last_result = {"ok": ok_n > 0, "dry": True, "items": results,
                             "note": f"{len(results)} 个场次里，{ok_n} 个现在能抢到"}
            return

        # ---------- 真抢 ----------
        if not released:
            first_try = plans[0].order()[:tries]
            is_pending = any(p.pending for p in plans)
            if is_pending:
                # 目标日期还没放场：点得再早也只是「占位」，真正发请求要等到
                # 放场前 2 秒。把这两个时刻都亮出来，免得对着屏幕猜它在干嘛。
                rel_local = dt.datetime.fromtimestamp(round(at - eng.offset))
                fire_local = rel_local - dt.timedelta(seconds=FIRE_LEAD)
                S.status = f"已就位，{fire_local:%H:%M:%S} 开始盯场"
                S.log(f"已就位，共 {len(plans)} 个场次（点「取消等待」可中止）。"
                      f"{rel_local:%H:%M:%S} 放场，{fire_local:%H:%M:%S} 才开始发只读查询；"
                      f"在那之前只对时，不碰抢场接口。")
            else:
                S.status = f"已就位，{dt.datetime.fromtimestamp(at):%m-%d %H:%M:%S} 发起"
                S.log(f"倒计时开始，共 {len(plans)} 个场次；第 1 场 {plans[0].slot} 依次尝试："
                      f"{' → '.join(first_try)}（点「取消等待」可中止）")
            # 等几小时再开抢时，服务器时钟可能在这一夜里跳变
            # （实测 ehall 的 Date 在几分钟内就差过 0.8 秒），
            # 所以开抢前 30 秒用最新读数重新对一次时，别拿几小时前的旧数据出手。
            # 一次对时要 4 秒左右，留够余量才值得做
            if at - time.time() > 40:
                eng.wait_until_server(at - 30, stop=lambda: S.cancel_requested)
                S.status = "开抢前重新对时"
                S.log("距开抢 30 秒，重新校一次时钟…")
                eng.sync_clock()
                S.clock = {"offset": round(eng.offset, 3), "err": round(eng.clock_err, 3),
                           "at": time.time()}
                if is_pending:
                    rel_local = dt.datetime.fromtimestamp(round(at - eng.offset))
                    S.status = (f"已就位，"
                                f"{(rel_local - dt.timedelta(seconds=FIRE_LEAD)):%H:%M:%S} 开始盯场")
                else:
                    S.status = f"已就位，{dt.datetime.fromtimestamp(at):%m-%d %H:%M:%S} 发起"

        results = []
        got: list[str] = []
        todo = []
        for p in plans:
            will_try = p.order()[:tries]
            if released and not will_try:
                # 全被约掉了，别浪费请求
                S.log(f"{p.slot}：所有场地都已被约，跳过不发请求")
                results.append({"slot": p.slot, "ok": False, "court": "",
                                "msg": "该时段已满场"})
                continue
            try_list = will_try or p.courts[:tries]
            S.log(f"{p.slot} 依次尝试：{' → '.join(try_list)}")
            todo.append(p)

        if todo:
            # 「同时发起」还是「逐个发起」，由界面上那个选项决定
            parallel = d.get("parallel", True) is not False
            shots: dict[str, dict] = {}
            if len(todo) == 1:
                shots[todo[0].slot] = eng.run(todo[0], at, max_tries=tries, gap_ms=gap,
                                              stop=lambda: S.cancel_requested)
            elif parallel:
                # 同时发起：所有场次在同一毫秒出手。
                # 一个个来，第二个场次就晚了一秒多，12:30 那一秒里晚一秒基本等于没抢。
                S.status = f"提交中（{len(todo)} 个场次同时发起）"
                S.log(f"{len(todo)} 个场次同时发起，各自只抢一片")
                shots = _fire_parallel(todo, at, tries, gap, eng)
            else:
                S.status = "提交中（逐个发起）"
                S.log(f"{len(todo)} 个场次逐个发起（更保守，但排在后面的会晚一点）")
                for i, p in enumerate(todo):
                    if S.cancel_requested:
                        S.log("已取消，后面还没轮到的场次不再提交")
                        break
                    if i:
                        time.sleep(0.25)      # 逐个发起就再拉开一点，别贴太紧
                    S.status = f"提交中（{i + 1}/{len(todo)}）{p.slot}"
                    shots[p.slot] = eng.run(p, at, max_tries=tries, gap_ms=gap,
                                            stop=lambda: S.cancel_requested)
                    if any(w in str((shots[p.slot] or {}).get("msg") or "")
                           for w in ("频繁", "稍后再试", "操作过快")):
                        S.log("遇到限流，停止后面所有场次")
                        break

            for p in todo:
                resp = shots.get(p.slot)
                if resp is None:
                    results.append({"slot": p.slot, "ok": False, "court": "",
                                    "msg": "已取消 / 没轮到就停了"})
                    continue
                ok = str(resp.get("code")) == "0"
                msg = str(resp.get("msg") or "")
                court = resp.get("court") or ""
                item = {"slot": p.slot, "ok": ok, "court": court, "msg": msg}
                if resp.get("uncertain"):
                    item["uncertain"] = True      # 超时：可能订上了，也可能没订上
                results.append(item)
                if ok:
                    got.append(f"{p.slot} {court}")
                    S.log(f"✔ {p.slot} 抢到 {court}")
                elif item.get("uncertain"):
                    S.log(f"⚠ {p.slot} 结果不确定：{msg}")
                else:
                    S.log(f"✘ {p.slot} 没抢到：{msg}")
                if any(w in msg for w in ("频繁", "稍后再试", "操作过快")):
                    S.throttled = True
                    S.log("注意：服务器判定操作频繁，账号进入冷却，请停止操作等它恢复。")

        ok_n = sum(1 for r in results if r["ok"])
        unsure = [r for r in results if r.get("uncertain")]
        first_msg = results[0]["msg"] if results else "无可用场地"
        S.last_result = {
            "ok": ok_n > 0,
            "items": results,
            "court": results[0]["court"] if results else "",
            "resp": {"msg": "、".join(got) if got else first_msg},
        }
        if unsure:
            S.last_result["uncertain"] = True
        if ok_n:
            S.status = f"抢到 {ok_n} 场：" + "、".join(got)
            if unsure:
                S.status += f"（另有 {len(unsure)} 场结果不确定，去「我的预约」看看）"
        elif unsure:
            S.status = "结果不确定，先去「我的预约」确认，别急着重抢"
        else:
            S.status = f"都没抢到：{first_msg}"
    except Exception as e:
        if str(e) == "已取消":
            S.log("已取消本次抢单，没有提交任何请求")
            S.status = "已取消"
            S.last_result = {}
        else:
            S.log(f"执行出错：{e}")
            S.status = f"出错：{e}"
            S.last_result = {"ok": False, "resp": {"msg": str(e)}}
    finally:
        S.firing = False
        S.busy = False


def serve(port: int = 0):
    srv = None
    for p_ in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p_), Handler)
            break
        except OSError:
            continue
    if srv is None:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


if __name__ == "__main__":
    boot()
    s, p = serve(8770)
    print(f"http://127.0.0.1:{p}/")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
