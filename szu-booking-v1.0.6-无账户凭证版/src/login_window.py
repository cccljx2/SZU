# -*- coding: utf-8 -*-
"""独立的登录窗口进程。

由主程序用 python.exe + CREATE_NO_WINDOW 拉起。
放在独立进程里，是因为 Playwright 在 pythonw（无控制台）下会硬崩，
崩了会连带把主程序一起带走。隔离之后主程序不受影响。
"""
import json
import pathlib
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "capture" / "login.log"

if sys.stdout is None:
    sys.stdout = open(ROOT / "capture" / "login_stdout.log", "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout


def note(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    note("登录进程启动")
    try:
        import accounts
        import login

        # 主程序可以把要用的配置目录作为第一个参数传进来
        profile = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else accounts.active_profile()
        note(f"配置目录：{profile}")
        info = login.do_login(note, profile=profile)
        if info:
            note("成功 " + json.dumps(info, ensure_ascii=False))
            return 0
        note("未完成登录")
        return 1
    except Exception:
        note("异常\n" + traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(main())
