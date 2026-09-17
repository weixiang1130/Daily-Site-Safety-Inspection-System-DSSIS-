# -*- coding: utf-8 -*-
"""cloud-runner 各腳本共用的啟動設定。**必須在 import app.* 之前 import。**

- 時區：全系統時間戳慣例是「台北時間、無時區註記」。GitHub 的主機跑 UTC，
  不在程式裡釘住的話時間會慢 8 小時、台北早上的「今天」會查到前一天。
- 資料庫：收集程式與模型都在 backend/onprem 底下，import 時就會依環境變數
  建立資料庫連線——一律指向用完即丟的暫存 SQLite，絕不連任何正式庫。
- 錯誤訊息遮蔽：公開 repo 的 Actions 日誌任何人可讀，requests 的錯誤字串含
  完整請求網址（出工試算表網址等同資料存取權），印出前一律隱去網址。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta

# 只在 POSIX 設 TZ：Windows 的 C 執行庫看不懂「Asia/Taipei」這種寫法，會把
# 它當成 UTC——本機（本來就是台北時區）反而被改錯 8 小時。Windows 沿用系統
# 時區，偏移不對時由 check_timezone 警告。
if hasattr(time, "tzset"):
    os.environ["TZ"] = "Asia/Taipei"
    time.tzset()

ONPREM = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "onprem"))
if ONPREM not in sys.path:
    sys.path.insert(0, ONPREM)

TMP_DB = os.path.join(tempfile.gettempdir(),
                      f"cloud-runner-{os.getpid()}-{int(time.time())}.db")
os.environ["DB_BACKEND"] = "sqlite"
os.environ["DATABASE_URL"] = "sqlite:///" + TMP_DB.replace("\\", "/")

_URL = re.compile(r"https?://[^\s）)」]+")


def redact(msg) -> str:
    """隱去網址。GitHub 只遮蔽與 Secret 完全相同的字串，轉換過的網址遮不到。"""
    return _URL.sub("<網址已隱藏>", str(msg))


def utf8_stdio() -> None:
    """輸出一律 UTF-8：Windows 本機 stdout 預設 cp950，遇到中文會整支炸掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def check_timezone() -> None:
    offset = datetime.now().astimezone().utcoffset()
    if offset != timedelta(hours=8):
        print(f"[警告] 目前時區偏移 {offset}，不是台北（+08:00）——時間戳與"
              f"「今天」的判定會錯。請以 TZ=Asia/Taipei 執行。", file=sys.stderr)


def cloud_endpoint(name: str) -> str:
    """由 CLOUD_INGEST_URL（…/api/v1/ingest/external）推出同層的其他推送端點，
    例如 cloud_endpoint("worklog") → …/api/v1/ingest/worklog，不必多設一個變數。"""
    base = (os.environ.get("CLOUD_INGEST_URL") or "").strip().rstrip("/")
    return base.rsplit("/", 1)[0] + "/" + name if base else ""


def remove_tmp_db() -> None:
    try:
        from app.db import engine
        engine.dispose()            # 先釋放連線，Windows 才刪得掉檔案
    except Exception:               # noqa: BLE001
        pass
    try:
        os.remove(TMP_DB)
    except OSError:
        pass
