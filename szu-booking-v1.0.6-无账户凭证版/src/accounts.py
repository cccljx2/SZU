# -*- coding: utf-8 -*-
"""账号管理：每个账号一个独立的浏览器配置目录，切换只改指针。

设计改动（2026-10-29）
----------------------
旧做法是「一个活动目录 + 切换时整目录搬移/删除」。实测有两个坑：
  1. 切换时要 rmtree 掉旧账号目录（几百个文件），在某些环境会触发
     批量删除保护，线程直接挂死，界面永远停在「正在切换账号…」。
  2. 搬移过程中如果浏览器还开着，文件被占用会失败，登录态就丢了。
新做法：`.profiles/<学号>/` 从一开始就各用各的，**切换只写一个指针文件**，
不删除、不搬移（唯一的一次 rename 发生在首次登录成功后给目录改名）。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILES = ROOT / ".profiles"
CAPTURE = ROOT / "capture"
USER = CAPTURE / "user.json"
COOK = CAPTURE / "cookies.json"
CURRENT = CAPTURE / "current.txt"        # 当前账号的学号
SCRATCH = PROFILES / "_new"              # 首次登录用的临时配置目录
LEGACY = ROOT / ".browser-profile"       # 老版本的单一目录，启动时自动迁移


# ---------- 身份信息 ----------
def read_user() -> dict:
    try:
        u = json.loads(USER.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not u.get("name"):
        u["name"] = (u.get("USER_INFO") or {}).get("name") or ""
    return u


def write_user(info: dict) -> None:
    CAPTURE.mkdir(parents=True, exist_ok=True)
    USER.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 当前账号 ----------
def current_uid() -> str:
    try:
        return CURRENT.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def set_current(uid: str) -> None:
    CAPTURE.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(uid or "", encoding="utf-8")


def profile_of(uid: str) -> pathlib.Path:
    return PROFILES / uid


def active_profile() -> pathlib.Path:
    """当前要用的浏览器配置目录。没有当前账号时，用临时目录等新登录。"""
    uid = current_uid()
    return profile_of(uid) if uid else SCRATCH


# ---------- 账号列表 ----------
def list_saved() -> list[dict]:
    if not PROFILES.exists():
        return []
    out = []
    for d in PROFILES.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        name = ""
        meta = d / "account.json"
        if meta.exists():
            try:
                name = json.loads(meta.read_text(encoding="utf-8")).get("name", "")
            except Exception:
                pass
        out.append({"uid": d.name, "name": name})
    return sorted(out, key=lambda x: x["uid"])


def register(uid: str, name: str = "") -> None:
    d = profile_of(uid)
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "account.json").write_text(
            json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def adopt_scratch(uid: str, name: str = "") -> str:
    """首次登录成功后，把临时目录改成以学号命名，并设为当前账号。

    整个过程只有 rename，不删除任何文件，所以不会触发批量删除保护。
    """
    PROFILES.mkdir(parents=True, exist_ok=True)
    dst = profile_of(uid)
    if SCRATCH.exists():
        if dst.exists():
            # 这个学号之前就有目录：先挪到备份名，不删
            bak = PROFILES / f"{uid}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os_rename(dst, bak)
            except Exception:
                pass
        try:
            os_rename(SCRATCH, dst)
        except Exception:
            pass
    register(uid, name)
    set_current(uid)
    return uid


def align(uid: str, name: str = "") -> str:
    """登录成功后校正「当前账号」，让目录名、current.txt、界面显示三者对齐。

    会不一致的原因：用 A 的浏览器配置登录了 B 的学号（手动输错了或者故意的）。
    这时配置目录还叫 A、current.txt 还写着 A，但实际登录的是 B，
    界面就出现「已登录 B，下拉框却显示 A（当前）」这种自相矛盾。

    这里只做改名：把配置目录从 A 改成 B，再把 current.txt 写成 B。
    不动里面的任何文件，所以不会丢登录态。
    """
    cur = current_uid()
    if not uid:
        return cur
    if uid == cur:
        register(uid, name)
        return uid

    src = profile_of(cur) if cur else SCRATCH
    dst = profile_of(uid)
    if src.exists() and src.resolve() != dst.resolve():
        if dst.exists():
            # 这个学号本来就有一份旧配置，先挪开备份，绝不覆盖
            bak = PROFILES / f"{uid}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os_rename(dst, bak)
            except Exception:
                return cur
        try:
            os_rename(src, dst)
        except Exception:
            # 改名没成功就什么都不动，免得 current.txt 指向一个空目录
            return cur
    register(uid, name)
    set_current(uid)
    return uid


def reconcile() -> str:
    """启动时校正一次：user.json 记的学号应当和 current.txt、目录名一致。

    不一致时界面会自相矛盾：状态栏显示「已登录 B」，下拉框却标着「A（当前）」。
    这里是只改名不删文件，失败了也不影响启动。
    """
    u = read_user()
    uid = str(u.get("USERID") or "").strip()
    if uid and uid != current_uid():
        return align(uid, u.get("name", ""))
    return current_uid()


def os_rename(src: pathlib.Path, dst: pathlib.Path) -> None:
    """纯改名（不跨盘、不复制、不删除）"""
    src.rename(dst)


def ensure_profiles() -> None:
    PROFILES.mkdir(parents=True, exist_ok=True)


def clear_credentials() -> None:
    """只删两个凭证文件（不会触发批量删除保护）"""
    for f in (USER, COOK):
        try:
            f.unlink()
        except Exception:
            pass


# ---------- 迁移老版本 ----------
def migrate_legacy() -> str:
    """把老版本的 .browser-profile 迁移成 .profiles/<学号>，返回提示信息"""
    if not LEGACY.exists():
        return ""
    ensure_profiles()
    u = read_user()
    uid = u.get("USERID")
    if uid and not profile_of(uid).exists():
        try:
            os_rename(LEGACY, profile_of(uid))
            register(uid, u.get("name", ""))
            set_current(uid)
            return f"已把现有登录态迁移到账号目录 {uid}"
        except Exception as e:
            return f"迁移登录态失败：{e}"
    if not current_uid():
        try:
            if SCRATCH.exists():
                bak = PROFILES / f"_new.bak-{time.strftime('%Y%m%d-%H%M%S')}"
                os_rename(SCRATCH, bak)
            os_rename(LEGACY, SCRATCH)
            return "已把现有登录态迁移为待分配账号"
        except Exception as e:
            return f"迁移登录态失败：{e}"
    return ""


if __name__ == "__main__":
    print("当前账号:", current_uid() or "（无）")
    print("配置目录:", active_profile())
    print("已保存账号:", list_saved())
    print("迁移:", migrate_legacy() or "无需迁移")
