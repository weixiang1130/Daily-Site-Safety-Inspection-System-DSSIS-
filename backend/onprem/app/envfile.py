"""讀取地端設定檔 .env.onprem。

為什麼要有這個檔
----------------
地端戰情室要連資料庫、監視器與氣象站，這些帳密不可能寫進程式碼（本 repo 公開），
也不適合叫使用者每次開機都手動設一堆環境變數——工地的電腦重開機是常態。
因此統一放在專案根目錄的 .env.onprem，已被 .gitignore 排除。

**必須在 app.main 讀取設定之前載入**，否則 main.py 模組層級的 os.environ.get()
會全部拿到空值，症狀是「設定明明填了卻沒生效」，而且不會有任何錯誤訊息。

刻意不用 python-dotenv：少一個相依，工地的電腦只要裝得起 FastAPI 就跑得動。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

# app/ → onprem/ → backend/ → 專案根目錄
ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT / ".env.onprem"

_loaded = False


def load_env() -> bool:
    """把 .env.onprem 讀進 os.environ。回傳是否真的讀到檔案。

    已存在的環境變數優先（用 setdefault），方便在不改檔的情況下臨時覆寫，
    測試時也才能指定不同的資料庫。
    """
    global _loaded
    if _loaded:
        return ENV_FILE.exists()
    _loaded = True

    if not ENV_FILE.exists():
        return False

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # 只切第一個 =，也不去掉行內的 #：密碼常含這兩個字元，
        # 自作聰明地清理會讓密碼悄悄變成錯的
        os.environ.setdefault(k.strip(), v.strip())
    return True


def env(key: str, default: str = "") -> str:
    load_env()
    return os.environ.get(key, default).strip()


def log(msg: str) -> None:
    """收集程式長期在背景執行，訊息一律帶時間，事後才查得出是哪一輪出的問題。"""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
