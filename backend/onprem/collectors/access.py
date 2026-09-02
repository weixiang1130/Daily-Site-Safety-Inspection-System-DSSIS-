"""進出場人次收集程式（公司門禁資料庫的唯讀檢視表）。

資料來源
--------
公司開放了一個唯讀檢視表，涵蓋目前所有列控工地，以 LocationName 區分工地。
本工地要看的是 ACCESS_SITE_LIKE 指定的關鍵字所涵蓋的門（例如 2 號門與 3 號門
分屬兩列，但同屬一個工地，必須合計）。

為什麼不寫死欄位名稱
--------------------
撰寫當下（2026-08-24）開發用的筆電連不到資料庫網段的 1433 埠——主機 ping 得到、
DNS 也解析得到，但 TCP 不通，是網段之間的防火牆規則。因此**欄位結構尚未實地
確認過**。照猜的欄位寫死，等於把整支程式建立在假設上，欄位名一錯就整份重寫。

所以這裡改成：連上之後先看實際有哪些欄位，再從候選名稱裡挑。挑不到就
**不寫任何資料並印出實際欄位**——寧可牆上顯示「尚無資料」，也不要把一個
猜出來的數字掛上去。疏散時有人會照著那個數字點名。

若自動判斷挑錯了，可用環境變數指定：

    ACCESS_COL_LOCATION   工地／地點欄位（預設自動判斷 LocationName）
    ACCESS_COL_DIRECTION  進出方向欄位
    ACCESS_COL_PERSON     人員識別欄位（用來去重，本程式不會儲存其內容）
    ACCESS_COL_TIME       時間欄位

用法（在 backend/onprem 目錄下執行）：

    python -m collectors.access            # 跑一輪就結束，用來驗證設定
    python -m collectors.access --loop     # 持續執行，交給工作排程器開機啟動

先確認結構請跑：python backend/tools/inspect_access_view.py（會遮蔽姓名與證號）

個資
----
門禁資料含姓名與員工編號，屬第三人個資。本程式**只寫入彙總後的人數**，
不把任何個人欄位存進本系統的資料庫，也不寫進記錄檔。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from app.db import DeviceReading, SessionLocal, Site, init_db

from .config import env, load_env, log

# 欄位候選名稱，由左至右優先。比對時一律轉小寫、去掉底線與空白，
# 因此 "Location_Name"、"locationName"、"LOCATION NAME" 都會命中。
CANDIDATES: Dict[str, List[str]] = {
    "location": ["locationname", "location", "sitename", "site", "gatename",
                 "doorname", "工地", "地點"],
    "direction": ["direction", "inout", "accesstype", "eventtype", "iotype",
                  "type", "進出", "方向"],
    # 識別碼類優先，姓名類排最後：只用來去重，能用編號就不要碰到姓名。
    # 無論挑到哪一個，本程式都只做 COUNT(DISTINCT)，不會儲存其內容。
    "person": ["personid", "employeeno", "empno", "userid", "cardno",
               "personno", "idno", "人員編號", "工號",
               "employee", "person", "user", "card"],
    "time": ["accesstime", "eventtime", "readtime", "recordtime", "createtime",
             "datetime", "time", "時間"],
}

# direction 欄位可能的「進場」表示法。實際值待現場確認，
# 這裡涵蓋常見寫法；判斷不出來時退回不分方向的總人數。
IN_VALUES = {"in", "i", "1", "進", "進場", "入", "entry", "enter"}
OUT_VALUES = {"out", "o", "0", "2", "出", "出場", "離", "exit", "leave"}


def norm(name: str) -> str:
    return name.lower().replace("_", "").replace(" ", "")


def pick(kind: str, columns: List[str]) -> Optional[str]:
    """從實際欄位中挑出某一類欄位。可用環境變數強制指定。"""
    forced = env(f"ACCESS_COL_{kind.upper()}")
    if forced:
        for c in columns:
            if norm(c) == norm(forced):
                return c
        log(f"設定的 ACCESS_COL_{kind.upper()}={forced} 不在檢視表欄位中，改用自動判斷")

    by_norm = {norm(c): c for c in columns}
    for cand in CANDIDATES[kind]:
        if cand in by_norm:
            return by_norm[cand]
    # 退一步做包含比對：例如 EmployeeNumber 命中 employeeno
    for cand in CANDIDATES[kind]:
        for n, original in by_norm.items():
            if cand in n:
                return original
    return None


def connect():
    try:
        import pyodbc
    except ImportError:
        sys.exit("需要 pyodbc，請先執行：pip install pyodbc")

    host, name = env("ACCESS_DB_HOST"), env("ACCESS_DB_NAME")
    if not host:
        return None

    # 依序嘗試已安裝的驅動程式；18 版預設要求加密，因此明確關閉憑證驗證
    last = ""
    for drv in ("ODBC Driver 18 for SQL Server",
                "ODBC Driver 17 for SQL Server",
                "SQL Server"):
        cs = (f"DRIVER={{{drv}}};SERVER={host};DATABASE={name};"
              f"UID={env('ACCESS_DB_USER')};PWD={env('ACCESS_DB_PASS')};"
              "TrustServerCertificate=yes;Connection Timeout=10")
        try:
            return pyodbc.connect(cs, timeout=10)
        except pyodbc.Error as e:
            last = str(e)[:150]
    raise RuntimeError(f"連不上門禁資料庫：{last}\n"
                       f"請確認這台機器連得到 {host} 的 1433 埠。")


def poll_once() -> str:
    if not env("ACCESS_DB_HOST"):
        return "未設定 ACCESS_DB_HOST，已跳過"

    view = env("ACCESS_DB_VIEW")
    site_like = env("ACCESS_SITE_LIKE")
    site_code = env("PRIMARY_SITE_CODE")
    if not site_like:
        return "未設定 ACCESS_SITE_LIKE，無法篩出本工地——不寫入任何資料"

    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT TOP 0 * FROM {view}")
        columns = [d[0] for d in cur.description]

        col_loc = pick("location", columns)
        if not col_loc:
            # 沒有地點欄位就無法只算本工地。全公司加總掛在本工地名稱下，
            # 比沒有數字更糟。
            return ("找不到地點欄位，無法篩出本工地，未寫入任何資料。\n"
                    f"檢視表實際欄位：{', '.join(columns)}\n"
                    "請用 ACCESS_COL_LOCATION 指定正確欄位。")

        col_dir = pick("direction", columns)
        col_person = pick("person", columns)

        where = f"WHERE {col_loc} LIKE ?"
        args = (f"%{site_like}%",)

        # 目前在場人數。有人員欄位就去重——同一人多次刷卡不該被算成多人。
        target = f"DISTINCT {col_person}" if col_person else "*"
        cur.execute(f"SELECT COUNT({target}) FROM {view} {where}", args)
        present = int(cur.fetchone()[0] or 0)

        metrics = {"headcount_present": present}
        # 把採用的欄位印出來。這張檢視表的語意（每列是「目前在場的人」還是
        # 「一筆進出紀錄」）尚未實地確認過；印出來才能讓第一次跑的人
        # 對照牆上的數字是否合理，而不是等到疏散時才發現不對。
        log(f"採用欄位：地點={col_loc}、方向={col_dir or '無'}、"
            f"人員={col_person or '無（未去重）'}；"
            f"篩選 {col_loc} LIKE %{site_like}%")

        # 進出場人次分開統計。方向欄位的實際值未經現場確認，
        # 對不上任何一組就不寫這兩項，只寫在場人數。
        if col_dir:
            cur.execute(
                f"SELECT {col_dir}, COUNT(*) FROM {view} {where} GROUP BY {col_dir}",
                args)
            rows = cur.fetchall()
            ins = sum(n for v, n in rows if str(v).strip().lower() in IN_VALUES)
            outs = sum(n for v, n in rows if str(v).strip().lower() in OUT_VALUES)
            if ins or outs:
                metrics["headcount_in"] = ins
                metrics["headcount_out"] = outs
            else:
                seen = sorted({str(v) for v, _ in rows})[:8]
                log(f"{col_dir} 的值 {seen} 對不上已知的進出表示法，"
                    "本輪只寫入在場人數")
    finally:
        conn.close()

    now = datetime.now()
    db = SessionLocal()
    try:
        site = (db.query(Site).filter(Site.code == site_code).first()
                if site_code else None)
        for metric, value in metrics.items():
            db.add(DeviceReading(
                site_id=site.id if site else None, site_code=site_code or None,
                vendor_code="access-db", device_type="people",
                device_id=site_code or "access", metric=metric, value_num=value,
                reading_at=now,
                # 刻意不存任何原始資料：這張檢視表含姓名與員工編號，
                # 本系統只需要人數
                raw_payload=None,
            ))
        db.commit()
    finally:
        db.close()

    return "、".join(f"{k}={v}" for k, v in metrics.items())


def main() -> None:
    load_env()
    init_db()
    interval = int(env("ACCESS_INTERVAL", "300") or 300)
    loop = "--loop" in sys.argv

    while True:
        try:
            log(poll_once())
        except Exception as e:                          # noqa: BLE001
            log(f"本輪失敗：{e}")
        if not loop:
            return
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已停止")
