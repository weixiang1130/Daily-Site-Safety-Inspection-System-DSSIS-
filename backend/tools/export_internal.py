# -*- coding: utf-8 -*-
"""匯出列控表工項成一個小檔，供工地檢視器每月更新「今日重點工項」。

用途（2026-09-02 起的唯一用途）
------------------------------
工地檢視器（docs/工地檢視器.md）打包時已烤入列控表工項；列控表改版時
不重發整個安裝包，改用這支在公司的機器上匯出一個約 150 KB 的檔，寄到
工地放進檢視器資料夾，點「更新工項.cmd」匯入。

**刻意只匯出工項，不匯出 FinOps 進度。** 工地版的設計原則是不顯示計價
數字（實際 vs 預估成本佔比），只顯示依工期推算的預定進度。早期版本曾
連 finops 進度一起匯出——工地一匯入，儀表板就會把該工地判定為「有實際
值」而顯示計價數字，且那個數字在工地永遠不會更新。這正是本工具改版的
原因，不要把 progress 加回來。

**不匯出 site_id。** 那是各台機器自己的流水號，兩邊不會一致；匯入端一律
用 site_code（BD04 這種代碼）重新對應。

缺失、照片、簽名、人員資料完全不在匯出範圍內——那些工地檢視器自己會從
填報站同步，不需要也不應該經過隨身碟。

用法
----
    python backend/tools/export_internal.py                  # 匯出全部工地
    python backend/tools/export_internal.py --site BD04      # 只匯出一個工地
    python backend/tools/export_internal.py -o D:/隨身碟/snap.internal.json

預設輸出檔名結尾是 `.internal.json`，已被 .gitignore 擋住，不會誤進版控。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "onprem"))

# schema 2：移除 progress 區塊（工地不得收到計價資料）。
# 匯入端把 schema 1 檔案裡的 progress 明確略過，不是靜默忽略。
SCHEMA = 2
DEFAULT_OUT = "internal_snapshot.internal.json"


def iso(v):
    """date / datetime → 字串；其餘原樣回傳。"""
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def main() -> None:
    ap = argparse.ArgumentParser(description="匯出列控表工項供工地檢視器更新")
    ap.add_argument("--site", help="只匯出這個工地代碼（預設全部）")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="輸出檔路徑")
    args = ap.parse_args()

    from app.envfile import load_env
    load_env()
    from app.db import PlannedTask, SessionLocal, init_db
    init_db()
    db = SessionLocal()
    try:
        tq = db.query(PlannedTask)
        if args.site:
            tq = tq.filter(PlannedTask.site_code == args.site)
        tasks = [{
            "site_code": t.site_code, "source": t.source,
            "task_no": t.task_no, "name": t.name,
            "start_date": iso(t.start_date), "end_date": iso(t.end_date),
            "outline_level": t.outline_level, "is_leaf": bool(t.is_leaf),
        } for t in tq.all()]
    finally:
        db.close()

    if not tasks:
        sys.exit("沒有任何工項可匯出。先跑 backend/tools/import_schedule.py "
                 "匯入列控表，再來匯出。")

    payload = {
        "schema": SCHEMA,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "planned_tasks": tasks,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # 摘要——帶到工地前先自己看一眼，免得帶了一份空檔或舊檔過去
    sites = sorted({t["site_code"] for t in tasks})
    size_kb = os.path.getsize(args.out) / 1024
    print(f"已匯出 → {args.out}（{size_kb:.0f} KB）")
    print(f"  工地：{'、'.join(sites)}")
    print(f"  工項：{len(tasks)} 筆")
    print("把這個檔放進工地檢視器資料夾，點「更新工項.cmd」即完成更新。")


if __name__ == "__main__":
    main()
