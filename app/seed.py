# -*- coding: utf-8 -*-
"""初始化資料庫：匯入 28 張檢查表模板、建立示範工地/廠商/帳號與示範資料。

用法：
    python -m app.seed          # 建表 + 匯入模板 + 建立示範帳號
    python -m app.seed --demo   # 另外塞入取自現行紙本的示範巡檢與缺失資料
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

from .auth import hash_password
from .db import (
    BASE_DIR, Coordination, CoordinationAttendee, Finding, FormItem, FormTemplate,
    Inspection, InspectionResult, SessionLocal, Site, User, Vendor, init_db,
)

FORMS_JSON = os.path.join(BASE_DIR, "data", "forms.json")

SITES = [
    ("SITE-A", "示範工地 A"),
    ("SITE-B", "示範工地 B"),
    ("SITE-C", "示範工地 C"),
]

VENDORS = [
    ("V001", "甲營造"),
    ("V002", "乙機電"),
    ("V003", "丙工程"),
    ("V004", "丁營造"),
    ("V005", "戊工程"),
    ("V006", "己工程"),
]

# username, password, 姓名, 角色, 工號, 工地代碼
USERS = [
    ("admin",  "admin1234",  "系統管理員",   "admin",     None,     None),
    ("pm01",   "pm1234",     "王專案",       "manager",   "EMP-001",  "SITE-A"),
    ("safe01", "safe1234",   "陳職安",       "safety",    "EMP-007",  "SITE-A"),
    ("eng01",  "eng1234",    "林工程師",     "engineer",  "EMP-012",  "SITE-A"),
    ("insp01", "insp1234",   "張小明",       "inspector", "EMP-081",  "SITE-C"),
    ("insp02", "insp1234",   "李小華",       "inspector", "EMP-137", "SITE-C"),
    ("insp03", "insp1234",   "吳大明",       "inspector", "EMP-055",  "SITE-B"),
]

# 示範用缺失內容（依實務常見樣態編寫，人名與廠商名均為虛構）
DEMO_FINDINGS = [
    ("施工架踏板倚靠欄杆未移除", "FALL", "墜落", "甲營造", "王小明", "onsite", "派員立即移除"),
    ("木材鋸台鋸齒未加裝安全防護", "CAUGHT", "被夾被捲", "乙機電", "陳小華", "onsite", "派員立即將鋸齒部分覆蓋改善"),
    ("施工架及上下設備未有安全防護", "FALL", "墜落", "丙工程", "林小美", "onsite", "派員立即改善"),
    ("電銲作業未設置滅火器", "FIRE", "火災", "丙工程", "林小美", "onsite", "派員立即設置滅火器"),
    ("預留鋼筋斷面未有安全防護", "PUNCTURE", "穿刺", "丁營造", "張小龍", "onsite", "派員立即加裝安全防護"),
    ("不合格施工架", "COLLAPSE", "倒塌崩塌", "丁營造", "李小強", "onsite", "派員立即移除"),
    ("施工架上下設備拆除未復原", "FALL", "墜落", "甲營造", "王小明", "onsite", "派員立即復原"),
]

# 示範用限期改善缺失（含逾期案例，供儀表板紅燈驗證）
DEMO_SCHEDULED = [
    ("地下室通高位置墜落風險，未設置護欄", "FALL", "墜落", "丁營造", "黃小天", 0),
    ("高架作業人員未確實佩掛全身式安全帶於堅固錨錠", "FALL", "墜落", "甲營造", "王小明", 1),
    ("分電盤未上鎖，非電氣人員可任意開啟", "ELEC", "感電", "乙機電", "陳小華", 2),
    ("工區出入口警示標誌破損未更換", "PPE", "門禁與防護具", "丁營造", "李小強", 3),
]


def seed_forms(db):
    with open(FORMS_JSON, encoding="utf-8") as f:
        data = json.load(f)
    n_form = n_item = 0
    for fm in data["forms"]:
        tpl = db.get(FormTemplate, fm["form_code"])
        if tpl is None:
            tpl = FormTemplate(form_code=fm["form_code"])
            db.add(tpl)
            n_form += 1
        tpl.title = fm["title"]
        tpl.short_name = fm["short_name"]
        tpl.form_type = fm["form_type"]
        tpl.item_count = fm["item_count"]
        tpl.active = True
        existing = {i.seq: i for i in db.query(FormItem)
                    .filter(FormItem.form_code == fm["form_code"]).all()}
        for it in fm["items"]:
            row = existing.get(it["seq"])
            if row is None:
                row = FormItem(form_code=fm["form_code"], seq=it["seq"])
                db.add(row)
                n_item += 1
            row.category = it["category"]
            row.hazard_code = it["hazard_code"]
            row.hazard_label = it["hazard_label"]
            row.text = it["text"]
    db.commit()
    print(f"表單模板：新增 {n_form} 張、{n_item} 個檢查項目"
          f"（共 {db.query(FormTemplate).count()} 張 / {db.query(FormItem).count()} 項）")


def seed_master(db):
    for code, name in SITES:
        if not db.query(Site).filter(Site.code == code).first():
            db.add(Site(code=code, name=name))
    for code, name in VENDORS:
        if not db.query(Vendor).filter(Vendor.code == code).first():
            db.add(Vendor(code=code, name=name))
    db.commit()

    site_by_code = {s.code: s.id for s in db.query(Site).all()}
    for username, pw, name, role, emp, site_code in USERS:
        u = db.query(User).filter(User.username == username).first()
        if u is None:
            u = User(username=username)
            db.add(u)
        u.password_hash = hash_password(pw)
        u.display_name = name
        u.role = role
        u.employee_no = emp
        u.site_id = site_by_code.get(site_code)
        u.active = True
    db.commit()
    print(f"主檔：{db.query(Site).count()} 個工地、{db.query(Vendor).count()} 家廠商、"
          f"{db.query(User).count()} 個帳號")


def seed_demo(db):
    """塞入示範資料，讓儀表板一開啟就有東西可看。"""
    if db.query(Finding).count() > 0:
        print("示範資料已存在，略過")
        return

    sites = {s.code: s for s in db.query(Site).all()}
    vendors = {v.name: v for v in db.query(Vendor).all()}
    users = {u.username: u for u in db.query(User).all()}
    today = date.today()

    # 1) 應變小組日報的當場改善缺失（分散在最近 10 天）
    for i, (desc, hz, hzl, vendor, person, action, fix) in enumerate(DEMO_FINDINGS * 3):
        d = today - timedelta(days=i % 10)
        db.add(Finding(
            site_id=sites["SITE-C"].id, source="daily_report",
            found_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=9 + i % 6),
            location=f"{(i % 5) + 1}F 施工區", hazard_code=hz, hazard_label=hzl,
            description=desc, vendor_id=vendors[vendor].id, responsible_person=person,
            severity="minor", action_type=action,
            fixed_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=10 + i % 6),
            fix_note=fix, status="closed", verified_at=datetime.combine(d, datetime.min.time()),
            verifier_id=users["safe01"].id, created_by=users["insp01"].id,
        ))

    # 2) 限期改善缺失（含逾期，讓儀表板紅燈有東西可亮）
    for j, (desc, hz, hzl, vendor, person, offset) in enumerate(DEMO_SCHEDULED):
        found = today - timedelta(days=6 - j)
        due = found + timedelta(days=offset + 1)
        overdue = due < today
        db.add(Finding(
            site_id=sites["SITE-B"].id, source="coordination",
            found_at=datetime.combine(found, datetime.min.time()) + timedelta(hours=14),
            location="地下室 B2", hazard_code=hz, hazard_label=hzl, description=desc,
            vendor_id=vendors[vendor].id, responsible_person=person,
            severity="major" if hz == "FALL" else "minor",
            action_type="scheduled", due_date=due,
            status="open" if overdue or j % 2 == 0 else "fixed",
            fixed_at=None if overdue or j % 2 == 0 else datetime.now(),
            created_by=users["insp03"].id,
        ))

    # 3) 一張已送出的一般作業自主檢查表
    tpl = db.get(FormTemplate, "F01")
    insp = Inspection(
        site_id=sites["SITE-B"].id, form_code="F01", inspect_date=today,
        location="地下室 B2 全區", weather="晴",
        inspector_id=users["insp03"].id, status="submitted", submitted_at=datetime.now(),
    )
    db.add(insp)
    db.flush()
    for k, item in enumerate(tpl.items):
        db.add(InspectionResult(
            inspection_id=insp.id, item_id=item.id,
            result="fail" if k in (5, 9) else "pass",
            remark="已請廠商當日改善" if k in (5, 9) else None,
        ))

    # 4) 一張每日協議巡視紀錄
    co = Coordination(
        site_id=sites["SITE-B"].id, meeting_date=today, work_date=today, weather="晴",
        agreement_text=("1. 高架作業人員須佩戴全身式安全帶，並將安全帶掛勾扣掛於堅固錨錠。\n"
                        "2. 天氣炎熱，人員須注意熱危害，工區提供飲用水；"
                        "身體不適應立即停止作業並回報管理職。"),
        patrol_text="詳見缺失清單。",
        handling_text="1. 地下室通高位置墜落風險，設置護欄。\n2. 檢查各樓層高處車輛及護欄設置狀況。",
        status="submitted", submitted_at=datetime.now(), created_by=users["insp03"].id,
    )
    db.add(co)
    db.flush()
    for wi, vn, trade, person in [
        ("鋼筋模板", "甲營造", "鋼筋", "吳小方"),
        ("機電", "乙機電", "機電", "鄭小雲"),
        ("排水工程", "丙工程", "排水", "謝小安"),
        ("鋼構組立", "丁營造", "鋼構", "蕭小文"),
    ]:
        db.add(CoordinationAttendee(
            coordination_id=co.id, work_item=wi, vendor_id=vendors[vn].id,
            vendor_name=vn, trade=trade, person_name=person, work_content=wi,
        ))

    db.commit()
    print(f"示範資料：{db.query(Finding).count()} 筆缺失、"
          f"{db.query(Inspection).count()} 張巡檢單、{db.query(Coordination).count()} 張協議單")


def main():
    init_db()
    db = SessionLocal()
    try:
        seed_forms(db)
        seed_master(db)
        if "--demo" in sys.argv:
            seed_demo(db)
    finally:
        db.close()
    print("\n完成。預設帳號："
          "\n  admin / admin1234        （系統管理員）"
          "\n  pm01  / pm1234           （工程專案主管）"
          "\n  safe01/ safe1234         （職安人員）"
          "\n  insp03/ insp1234         （檢查人員 SITE-B）")


if __name__ == "__main__":
    main()
