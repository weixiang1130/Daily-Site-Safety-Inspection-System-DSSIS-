# -*- coding: utf-8 -*-
"""檢視目前資料庫的結構與筆數，用於確認 DDL 與資料正確落地。"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'backend', 'onprem'))

from sqlalchemy import inspect, text  # noqa: E402

from app.db import Base, engine, db_info  # noqa: E402

print("連線：", db_info())
insp = inspect(engine)
tables = sorted(insp.get_table_names())
print(f"\n共 {len(tables)} 張資料表\n" + "=" * 78)

with engine.connect() as cn:
    for t in tables:
        n = cn.execute(text(f"SELECT COUNT(*) FROM [{t}]")).scalar()
        cols = insp.get_columns(t)
        pk = insp.get_pk_constraint(t).get("constrained_columns", [])
        fks = insp.get_foreign_keys(t)
        print(f"\n■ {t}　（{n} 筆，{len(cols)} 欄）")
        for c in cols:
            flags = []
            if c["name"] in pk:
                flags.append("PK")
            if not c["nullable"]:
                flags.append("NOT NULL")
            fk = next((f for f in fks if c["name"] in f["constrained_columns"]), None)
            if fk:
                flags.append(f"→ {fk['referred_table']}.{fk['referred_columns'][0]}")
            print(f"    {c['name']:<20} {str(c['type']):<18} {' '.join(flags)}")

    print("\n" + "=" * 78)
    print("中文資料抽樣驗證：")
    for sql in (
        "SELECT TOP 3 title, form_type, item_count FROM form_templates ORDER BY form_code",
        "SELECT TOP 3 category, hazard_label, LEFT(text, 30) FROM form_items ORDER BY id",
        "SELECT TOP 3 hazard_label, LEFT(description, 28), responsible_person "
        "FROM findings ORDER BY id",
    ):
        try:
            for row in cn.execute(text(sql)):
                print("   ", " | ".join(str(v) for v in row))
        except Exception as e:                     # SQLite 不支援 TOP
            print("   （略過：", e.__class__.__name__, "）")
        print()
