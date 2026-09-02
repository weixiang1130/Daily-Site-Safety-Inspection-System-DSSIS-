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

# 兩個候選檔名：.env.onprem 是開發與地端主機用的慣例名；
# 「連線設定.env」是工地檢視器安裝包用的名字——工地的人打開資料夾
# 要一眼看得出哪個檔是設定，點號開頭的隱藏檔式命名對他們是障礙。
# 兩個都在就取 .env.onprem（開發機可能同時有測試用的兩份）。
_CANDIDATES = (ROOT / ".env.onprem", ROOT / "連線設定.env")
ENV_FILE = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

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

    # utf-8-sig：檔案若被記事本存成「UTF-8 (BOM)」，用純 utf-8 讀第一個
    # 鍵會黏著一個 U+FEFF，比對永遠不中——症狀是「第一行的設定莫名沒生效」
    for k, v in parse_env(ENV_FILE.read_text(encoding="utf-8-sig")).items():
        os.environ.setdefault(k, v)
    return True


def parse_env(text: str) -> dict:
    """解析 .env 內容成 dict。

    這是唯一的一份解析規則——打包工具（backend/tools/make_site_package.py）
    也用它讀設定代填進安裝包。兩邊各寫一份的話，這裡修了邊角案例
    （例如含 # 的密碼）那邊不會跟上，烤進包裡的憑證就默默解析得不一樣。
    """
    values: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # 只切第一個 =，也不去掉行內的 #：密碼常含這兩個字元，
        # 自作聰明地清理會讓密碼悄悄變成錯的
        values[k.strip()] = v.strip()
    return values


def env(key: str, default: str = "") -> str:
    load_env()
    return os.environ.get(key, default).strip()


def log(msg: str) -> None:
    """收集程式長期在背景執行，訊息一律帶時間，事後才查得出是哪一輪出的問題。"""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
