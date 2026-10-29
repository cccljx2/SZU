# -*- coding: utf-8 -*-
"""登录模块：打开独立浏览器让用户登录，保存 cookie 与身份信息

每个账号用自己独立的浏览器配置目录（见 accounts.py），
这里的 profile 参数就是那个目录。
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Callable

from playwright.sync_api import sync_playwright

from api import ROOT, INDEX_URL

COOKIE_FILE = ROOT / "capture" / "cookies.json"
USER_FILE = ROOT / "capture" / "user.json"

APP_URL = INDEX_URL + "#/sportVenue"

INFO_JS = """() => ({
    USERID: window.USERID,
    name: (window.USER_INFO && window.USER_INFO.name) || '',
    userName: (window.USER_INFO && window.USER_INFO.userName) || '',
    dept: (window.USER_INFO && window.USER_INFO.deptName) || ''
})"""


def _default_profile() -> pathlib.Path:
    import accounts
    return accounts.active_profile()


def _save(ctx, info: dict) -> None:
    cookies = ctx.cookies()
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
    USER_FILE.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")


def refresh_session(profile: pathlib.Path | str | None = None,
                    timeout: int = 30) -> dict | None:
    """不弹窗，用现有登录态静默取回 cookie 与身份（切回已存账号时用）"""
    import accounts
    profile = pathlib.Path(profile) if profile else _default_profile()
    if not profile.exists():
        return None
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), channel="msedge", headless=True, viewport=None)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_function("() => window.USERID || ''", timeout=timeout * 1000)
            except Exception:
                return None
            info = page.evaluate(INFO_JS)
            _save(ctx, info)
            accounts.write_user(info)
            return info
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def do_login(log: Callable[[str], None] = print, timeout: int = 300,
             profile: pathlib.Path | str | None = None) -> dict | None:
    """弹出浏览器等待用户登录，成功后返回用户信息"""
    profile = pathlib.Path(profile) if profile else _default_profile()
    log(f"正在打开登录窗口…（配置目录 {profile.name}）")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), channel="msedge", headless=False, viewport=None,
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + timeout
        info = None
        while time.time() < deadline:
            try:
                if not ctx.pages:
                    break
                uid = page.evaluate("() => window.USERID || ''")
                if uid:
                    page.wait_for_timeout(1500)
                    info = page.evaluate(INFO_JS)
                    break
            except Exception:
                pass
            page.wait_for_timeout(800)

        if info:
            _save(ctx, info)
            log(f"登录成功：{info.get('name')}（{info.get('USERID')}）")
        else:
            log("登录未完成")
        try:
            ctx.close()
        except Exception:
            pass
        return info


def load_user() -> dict:
    if USER_FILE.exists():
        try:
            return json.loads(USER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    do_login()
