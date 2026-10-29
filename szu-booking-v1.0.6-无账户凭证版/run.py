# -*- coding: utf-8 -*-
"""启动：本地服务 + Edge 应用窗口"""
from __future__ import annotations

import os
import pathlib
import atexit
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent

# pythonw 启动时没有控制台，sys.stdout 为 None，任何 print 都会让程序直接崩掉。
# 这里先接管输出，保证后台运行安全。
if sys.stdout is None or sys.stderr is None:
    try:
        (ROOT / "capture").mkdir(parents=True, exist_ok=True)
        _f = open(ROOT / "capture" / "stdout.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _f
        if sys.stderr is None:
            sys.stderr = _f
    except Exception:
        pass

sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _alive_mark(port: int) -> None:
    try:
        (ROOT / "capture" / "alive.txt").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} port={port}", encoding="utf-8")
    except Exception:
        pass


def _open_window(url: str) -> None:
    if os.environ.get("SZU_NO_WINDOW"):
        print("跳过打开窗口（SZU_NO_WINDOW=1）")
        return
    edge = find_edge()
    if edge:
        subprocess.Popen([
            edge,
            f"--app={url}",
            "--window-size=1000,900",
            "--window-position=120,40",
        ])
    else:
        import webbrowser
        webbrowser.open(url)


def _already_running(port: int = 8788) -> bool:
    """端口上已经有我们的服务吗？

    同时开两个实例会很危险：两个都到了 12:30 各自提交一次，可能订到两片不同的场地。
    所以第二个实例不再启动调度，只把已有窗口叫出来。
    """
    import json as _json
    import urllib.request
    # 必须绕开系统代理：装过 Clash / 全局代理的机器会把 127.0.0.1 也送去代理，
    # 结果这里一直判「没在运行」，第二个实例就照样起来抢 8789 端口、弹第二个窗口。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            d = _json.loads(r.read().decode("utf-8"))
        return isinstance(d, dict) and "status" in d and "logs" in d
    except Exception:
        return False


_LOCK_FH = None       # 锁文件句柄，必须一直活着，进程退出时系统自动释放


def _take_lock() -> bool:
    """抢一个独占锁，保证同一台机器上只有一个实例在跑。

    不能只靠端口判断：Windows 的 SO_REUSEADDR 语义和 Linux 不一样，
    两个进程能同时绑住同一个 8788，谁接管进来的连接说不准，
    结果就是两个窗口、一个有账号一个没有。
    """
    global _LOCK_FH
    try:
        (ROOT / "capture").mkdir(parents=True, exist_ok=True)
        fh = open(ROOT / "capture" / "app.lock", "a+b")
        fh.seek(0)
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_FH = fh
        return True
    except OSError:
        return False
    except Exception:
        return True     # 拿不到锁机制（非 Windows 之类）就别拦着程序启动


def _start_stamp() -> float:
    """上一个实例是什么时候起来的（时间戳，没有就返回 0）"""
    try:
        return float((ROOT / "capture" / "start.txt").read_text(encoding="utf-8").strip())
    except Exception:
        return 0.0


def _write_start_stamp() -> None:
    try:
        (ROOT / "capture").mkdir(parents=True, exist_ok=True)
        (ROOT / "capture" / "start.txt").write_text(f"{time.time():.3f}", encoding="utf-8")
    except Exception:
        pass


def _handover(port: int = 8788) -> None:
    """已经有实例在跑：不再启动调度，也不再另开窗口。

    只在「上一个实例是好几秒前起来的」时候才补一个窗口——
    那种情况是用户把窗口关了又想打开，得给他一个；
    如果是同一秒内的重复双击（启动.bat 重试、手抖点两下），
    那个实例自己的窗口已经在了，再开就是两个窗口。
    """
    stamp = _start_stamp()
    print(f"已有实例在运行，端口 {port}")
    # 看不到启动时间，说明另一个实例这会儿正在启动（还没走到写时间戳那一步），
    # 它的窗口马上就会弹出来，这里别再弹一个。
    if not stamp:
        print("对方正在启动中，不再重复弹窗口")
        return
    age = time.time() - stamp
    if age > 20:
        print(f"上一个实例是 {age:.0f} 秒前启动的，补开一个窗口指向它")
        _open_window(f"http://127.0.0.1:{port}/")
    else:
        print(f"刚启动的实例（{age:.1f} 秒前）已经弹过窗口了，这里不再重复弹")


def main():
    if not _take_lock():
        print("本目录已经有一个实例在跑")
        _handover(8788)
        return

    # 抢到锁就立刻记下启动时间，后面被让位的那次靠它判断要不要补窗口
    _write_start_stamp()

    # 锁在自己手上，说明本目录没有别的实例。这里 8788 还有响应，
    # 那就是旧版本、或者另一个文件夹里的副本在跑——我们不再起服务，
    # 但得给用户一个窗口，否则他双击之后什么也看不到。
    if _already_running(8788):
        print("8788 上已经有程序在运行（旧版本或另一个文件夹的副本），直接打开它的窗口")
        _open_window("http://127.0.0.1:8788/")
        return

    server.boot()
    srv, port = server.serve(8788)

    # 同时双击两次时，后起来的那个会落到 8789，于是弹出第二个窗口，
    # 而且那个窗口是独立实例（没登录、没账号）。这里直接让位，只留一个窗口。
    if port != 8788:
        # 两个实例几乎同时启动时，另一个可能还没把端口挂上，
        # 所以隔一会儿再确认一次，别急着抢。
        for _ in range(30):
            if _already_running(8788):
                break
            time.sleep(0.1)
        if _already_running(8788):
            srv.shutdown()
            _handover(8788)
            return

    url = f"http://127.0.0.1:{port}/"
    print("服务已启动:", url)
    try:
        (ROOT / "capture").mkdir(parents=True, exist_ok=True)
        (ROOT / "capture" / "port.txt").write_text(str(port), encoding="utf-8")
    except Exception:
        pass

    _open_window(url)

    atexit.register(lambda: server.S.log("主进程正常退出"))
    try:
        while True:
            _alive_mark(port)
            time.sleep(10)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
