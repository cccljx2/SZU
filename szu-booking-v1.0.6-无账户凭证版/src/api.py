# -*- coding: utf-8 -*-
"""深大体育场馆预约 - 接口客户端

所有接口都走 /qljfwapp/sys/lwSzuCgyy/sportVenue/xxx.do，
鉴权仅依赖 ehall.szu.edu.cn 的登录 cookie（无 token、无签名）。
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

BASE = "https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy"
VENUE = BASE + "/sportVenue"
MODULE = BASE + "/modules/sportVenue"
INDEX_URL = BASE + "/index.do"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0")

ROOT = pathlib.Path(__file__).resolve().parent.parent
COOKIE_FILE = ROOT / "capture" / "cookies.json"


class ApiError(RuntimeError):
    pass


class SzuClient:
    def __init__(self, cookie_file: pathlib.Path | str | None = None, timeout: float = 12.0):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": INDEX_URL,
            "Origin": "https://ehall.szu.edu.cn",
        })
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        self.s.mount("https://", adapter)
        if cookie_file:
            self.load_cookies(cookie_file)

    # ---------- 会话 ----------
    def load_cookies(self, path: pathlib.Path | str) -> int:
        path = pathlib.Path(path)
        if not path.exists():
            raise ApiError(f"cookie 文件不存在: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        for c in data:
            if "szu.edu.cn" not in c.get("domain", ""):
                continue
            self.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
            n += 1
        return n

    def save_cookies(self, path: pathlib.Path | str) -> int:
        jar = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
               for c in self.s.cookies]
        pathlib.Path(path).write_text(json.dumps(jar, ensure_ascii=False, indent=1), encoding="utf-8")
        return len(jar)

    def is_logged_in(self) -> bool:
        try:
            d = self.venue_data()
        except Exception:
            return False
        return str(d.get("code")) == "0"

    # ---------- 业务接口 ----------
    def _post(self, path: str, data: dict | None = None, base: str | None = None) -> Any:
        url = f"{base or VENUE}/{path}"
        r = self.s.post(url, data=data or None, timeout=self.timeout)
        if r.status_code != 200:
            raise ApiError(f"{path} HTTP {r.status_code}")
        txt = r.text.strip()
        if not txt:
            raise ApiError(f"{path} 返回空")
        try:
            return r.json()
        except ValueError:
            # 未登录时会被重定向到 CAS 登录页（HTML）
            raise ApiError(f"{path} 返回非 JSON，登录可能已失效")

    def venue_data(self) -> dict:
        return self._post("getSportVenueData.do")

    def date_list(self) -> list[str]:
        d = self._post("getRqList.do")
        if isinstance(d, list):
            return d
        return d.get("datas") or d.get("data") or []

    def time_list(self, xq: str, rq: str, yylx: str = "1.0", xmdm: str = "001") -> list[dict]:
        d = self._post("getTimeList.do", {"XQ": xq, "YYRQ": rq, "YYLX": yylx, "XMDM": xmdm})
        if isinstance(d, list):
            return d
        if str(d.get("code")) not in ("0", "None"):
            raise ApiError(f"getTimeList: {d.get('msg')}")
        return d.get("datas") or []

    def opening_rooms(self, *, xmdm: str, rq: str, yylx: str, kssj: str, jssj: str,
                      xqdm: str | None = None, cgbm: str | None = None) -> list[dict]:
        payload = {"XMDM": xmdm, "YYRQ": rq, "YYLX": yylx, "KSSJ": kssj, "JSSJ": jssj}
        if xqdm:
            payload["XQDM"] = xqdm
        if cgbm:
            payload["CGBM"] = cgbm
        d = self._post("getOpeningRoom.do", payload, base=MODULE)
        if str(d.get("code")) != "0":
            raise ApiError(f"getOpeningRoom: {d.get('msg')}")
        return (((d.get("datas") or {}).get("getOpeningRoom") or {}).get("rows")) or []

    def insert_booking(self, form: dict) -> dict:
        return self._post("insertVenueBookingInfo.do", form)


def build_form(room: dict, rq: str, slot: str, yylx: str, user_id: str, user_name: str,
               cyrs: int = 1) -> dict:
    """完全对齐网页 save() 的拼装逻辑。

    注意：真实场地行里没有 CQBM 字段（前端 formData.CQBM 取到 undefined，
    jQuery 序列化时会直接丢掉这个键），所以这里也不发，避免多带一个空值。
    """
    ks, js = slot.split("-")
    form = {
        "DHID": "",
        "YYRGH": user_id,
        "YYRXM": user_name,
        "CYRS": str(cyrs),
        "CGDM": room.get("CGBM", ""),
        "CDWID": room.get("WID", ""),
        "XMDM": room.get("XMDM", ""),
        "XQWID": room.get("XQDM", ""),
        "KYYSJD": slot,
        "YYRQ": rq,
        "YYLX": yylx,
        "YYKS": f"{rq} {ks}",
        "YYJS": f"{rq} {js}",
        "PC_OR_PHONE": "pc",
    }
    if room.get("CQBM"):
        form["CQBM"] = room["CQBM"]
    return form


if __name__ == "__main__":
    c = SzuClient(COOKIE_FILE)
    print("登录态:", c.is_logged_in())
    print("可选日期:", c.date_list())
