# -*- coding: utf-8 -*-
"""把 export_internal.py 產出的工項檔匯入本機資料庫（工地檢視器每月更新用）。

搭配 backend/tools/export_internal.py 使用——為什麼這樣搬，看那支的說明。
在工地檢視器上由「更新工項.cmd」呼叫；也可手動執行：

    python backend/tools/import_internal.py snap.internal.json --dry-run
    python backend/tools/import_internal.py snap.internal.json

工項採**整份替換**：列控表每月改版，工項名稱會改、識別碼會重排，逐筆
比對沒有可靠的鍵。只替換檔案裡有出現的 (工地, 來源) 組合，不動其他工地。

**進度資料一律拒收。** 舊版（schema 1）的匯出檔含 FinOps 計價進度；工地
檢視器不得顯示計價數字（設計原則見 docs/工地檢視器.md），因此就算收到
舊版檔案，這裡也明確略過 progress 並印出原因——不是靜默忽略，操作者要
知道那塊沒有進來、也不該進來。

工地代碼對應不到本機工地時**跳過並警告，不自動建立**。工地主檔是由填報
站同步下來的，這裡自己造一筆會產生一個與雲端對不起來的孤兒工地。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "onprem"))

# 接受的格式版本。schema 1 是含 progress 的舊版——工項部分照收，
# progress 部分拒收（見模組說明）。
KNOWN_SCHEMAS = (1, 2)


def as_date(s) -> date:
    return date.fromisoformat(str(s)[:10])


def main() -> None:
    ap = argparse.ArgumentParser(description="匯入列控表工項")
    ap.add_argument("path", help="export_internal.py 產出的 .internal.json")
    ap.add_argument("--dry-run", action="store_true", help="只列出將匯入的內容")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        sys.exit(f"找不到檔案：{args.path}")
    with open(args.path, encoding="utf-8") as f:
        data = json.load(f)

    if data.get("schema") not in KNOWN_SCHEMAS:
        sys.exit(f"檔案格式版本是 {data.get('schema')}，這支工具只認得 "
                 f"{KNOWN_SCHEMAS}。兩邊的程式碼版本可能不一致，"
                 "先把 repo 更新到同一版。")

    tasks = data.get("planned_tasks") or []
    groups = sorted({(t["site_code"], t["source"]) for t in tasks})

    print(f"檔案匯出時間：{data.get('exported_at', '未知')}")
    print(f"  工項：{len(tasks)} 筆，將整份替換 "
          f"{'、'.join(f'{s}/{src}' for s, src in groups) or '無'}")
    if data.get("progress"):
        print(f"  [注意] 檔案含 {len(data['progress'])} 筆計價進度（舊版格式），"
              "已略過不匯入——工地檢視器不顯示計價數字，"
              "牆上的進度一律以列控表工期推算。")
    if args.dry_run:
        print("（dry-run，未寫入）")
        return

    # 先讀設定檔再 import db——db.py 在 import 時就決定連線字串，
    # 順序反了會默默連到錯的資料庫（工地檢視器用 SQLite，設定在檔案裡）
    from app.envfile import load_env
    load_env()
    from app.db import PlannedTask, SessionLocal, Site, init_db
    init_db()
    db = SessionLocal()
    try:
        sites = {s.code: s.id for s in db.query(Site).all()}
        missing = sorted({t["site_code"] for t in tasks} - set(sites))
        if missing:
            print(f"[注意] 本機沒有這些工地，相關工項已略過：{'、'.join(missing)}"
                  "\n  工地主檔是從填報站同步下來的，等首輪同步完成後再匯一次。")

        replaced = added = 0
        for site_code, source in groups:
            if site_code not in sites:
                continue
            replaced += (db.query(PlannedTask)
                         .filter(PlannedTask.site_code == site_code,
                                 PlannedTask.source == source).delete())
        for t in tasks:
            if t["site_code"] not in sites:
                continue
            db.add(PlannedTask(
                site_id=sites[t["site_code"]], site_code=t["site_code"],
                source=t["source"], task_no=t.get("task_no"), name=t["name"],
                start_date=as_date(t["start_date"]),
                end_date=as_date(t["end_date"]),
                outline_level=t.get("outline_level"),
                is_leaf=bool(t.get("is_leaf", True)),
            ))
            added += 1
        db.commit()
        print(f"工項：刪除舊資料 {replaced} 筆 → 寫入 {added} 筆")
        print("重新整理儀表板即可看到。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
