# -*- coding: utf-8 -*-
"""把標準「每日安全作業循環」匯入雲端看板設定（免逐段手打）。

工地看板的「安全作業循環」時鐘依這張時程表顯示彩色時段環。這支工具
一次寫入一整套標準流程，省去在看板管理頁逐段新增。

安全性
------
密碼用 getpass 讀取——輸入時不顯示、不寫進檔案、不留在指令歷史。
工具本身不含任何帳密；站台網址讀自 .env.onprem 的 CLOUD_API_URL。

只覆蓋 schedule 一段，聯絡人／公告／無災害起算等其餘設定原封不動；
可重複執行（例如日後要調整時段）。

用法（在 backend/onprem 目錄下執行，才讀得到同層的 .env.onprem）：

    python ../tools/seed_board_schedule.py

會提示輸入雲端管理員帳密，登入後寫入主場站的每日作業循環。
"""
from __future__ import annotations

import getpass
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "onprem")))

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.envfile import load_env

# 標準每日安全作業循環。作業名只要含關鍵字（安全作業／午休／休息／
# 會議／檢討／檢點／整理…）看板時鐘就會自動上對應顏色。時段不可重疊。
SCHEDULE = [
    ("08:00", "08:05", "安全早會"),
    ("08:05", "08:15", "工具箱會議（危害預知）"),
    ("08:15", "08:20", "作業前檢點"),
    ("08:20", "10:00", "安全作業"),
    ("10:00", "10:10", "上午休息"),
    ("10:10", "12:00", "安全作業"),
    ("12:00", "13:00", "午休"),
    ("13:00", "13:10", "下午作業前檢點"),
    ("13:10", "15:00", "安全作業"),
    ("15:00", "15:10", "下午休息"),
    ("15:10", "16:30", "安全流程檢討"),
    ("16:30", "17:00", "整理整頓及清掃"),
]

TIMEOUT = 30


def main() -> None:
    load_env()
    base = os.environ.get("CLOUD_API_URL", "").rstrip("/")
    if not base:
        sys.exit("找不到 CLOUD_API_URL。請在 backend/onprem 目錄下執行"
                 "（才讀得到 .env.onprem），或先設定該環境變數。")

    print(f"雲端站台：{base}")
    user = input("雲端管理員帳號：").strip()
    pw = getpass.getpass("密碼（輸入時不會顯示）：")

    s = requests.Session()
    r = s.post(f"{base}/api/login", data={"username": user, "password": pw}, timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"登入失敗（HTTP {r.status_code}）：{r.text[:120]}")

    brand = s.get(f"{base}/api/branding", timeout=TIMEOUT).json()
    code = (brand.get("primary_site_code") or "").strip()
    sites = s.get(f"{base}/api/board-sites", timeout=TIMEOUT).json()
    site = next((x for x in sites if x["code"] == code), sites[0] if sites else None)
    if not site:
        sys.exit("找不到主場站（PRIMARY_SITE_CODE 對不上，或雲端沒有啟用中的工地）。")

    cur = s.get(f"{base}/api/site-board/{site['id']}", timeout=TIMEOUT).json()
    cfg = cur["config"]
    cfg["schedule"] = [{"start": a, "end": b, "label": c} for a, b, c in SCHEDULE]

    r = s.post(f"{base}/api/site-board/{site['id']}",
               json={"revision": cur["revision"], "config": cfg}, timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"寫入失敗（HTTP {r.status_code}）：{r.text[:200]}")

    print(f"完成：「{site['name']}」已寫入 {len(SCHEDULE)} 段每日作業循環"
          f"（版本 {r.json().get('revision')}）。")
    print("最多 15 分鐘後（下一次快照）雲端看板就會出現彩色時鐘。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消")
