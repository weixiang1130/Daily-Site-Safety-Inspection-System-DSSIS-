# -*- coding: utf-8 -*-
"""把本機資料庫的「工地主檔＋列控表工項」烤進一個 SQLite 檔，供工地檢視器打包用。

工地檢視器第一次啟動時，表單同步要等雲端回應、環境與人數要等第一輪輪詢，
但「今日重點工項」的來源（列控表）是打包這台機器才有的東西——不烤進去，
工地那格會永遠空白。工地主檔（sites）也一起烤：工項靠 site_code 對應工地，
主檔晚到的話工項會先顯示成代碼。

其餘資料（缺失、環境、人數）刻意**不烤**：那些是活資料，烤進去只會讓
工地第一眼看到打包當天的舊數字，還以為是現況。空白比過時誠實。

用法（由 make_site_package.py 呼叫，也可手動跑）
------------------------------------------------
    python backend/tools/bake_viewer_db.py <輸出.sqlite>
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "onprem"))


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("用法：python backend/tools/bake_viewer_db.py <輸出.sqlite>")
    out = os.path.abspath(sys.argv[1])

    from app.envfile import load_env
    load_env()      # 來源資料庫（本機）的連線設定

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base, PlannedTask, SessionLocal, Site

    src = SessionLocal()
    sites = src.query(Site).all()
    tasks = src.query(PlannedTask).all()
    src.close()
    if not tasks:
        sys.exit("本機資料庫沒有任何工項——先跑 backend/tools/import_schedule.py "
                 "匯入列控表，再打包。")

    if os.path.exists(out):
        os.remove(out)
    dest_engine = create_engine("sqlite:///" + out.replace("\\", "/"))
    Base.metadata.create_all(dest_engine)

    with Session(dest_engine) as dest:
        # id 原樣照搬：planned_tasks.site_id 指向 sites.id，兩表一起搬才對得上
        for s in sites:
            dest.add(Site(id=s.id, code=s.code, name=s.name, active=s.active,
                          department=s.department, sort_order=s.sort_order))
        for t in tasks:
            dest.add(PlannedTask(
                site_id=t.site_id, site_code=t.site_code, source=t.source,
                task_no=t.task_no, name=t.name, start_date=t.start_date,
                end_date=t.end_date, outline_level=t.outline_level,
                is_leaf=t.is_leaf))
        dest.commit()

    print(f"已烤入 {len(sites)} 個工地、{len(tasks)} 筆工項 → {out}")


if __name__ == "__main__":
    main()
