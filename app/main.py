# -*- coding: utf-8 -*-
"""職安填報系統 API。

啟動：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import base64
import os
import re
import uuid
from datetime import date, datetime, timedelta

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import SECRET_KEY, authenticate, current_user
from .db import (
    BASE_DIR, Coordination, CoordinationAttendee, DeviceReading, Finding, FormItem,
    FormTemplate, Inspection, InspectionResult, SessionLocal, Signature, Site, User,
    Vendor, db_info, init_db,
)
from .pdf import build_coordination_pdf, build_inspection_pdf

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
PHOTO_DIR = os.path.join(UPLOAD_DIR, "photos")
SIG_DIR = os.path.join(UPLOAD_DIR, "signatures")
for d in (PHOTO_DIR, SIG_DIR):
    os.makedirs(d, exist_ok=True)

# 設備廠商推送資料用的權杖。正式環境請改用環境變數，並一家廠商一組。
INGEST_TOKENS = {
    t.split(":")[0]: t.split(":")[1]
    for t in os.environ.get("INGEST_TOKENS", "vendor-a:demo-token-vendor-a,"
                                             "vendor-b:demo-token-vendor-b,"
                                             "vendor-c:demo-token-vendor-c").split(",")
    if ":" in t
}

app = FastAPI(title="職安填報系統", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=12 * 3600)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def need_login(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="請先登入")
    return user


def _save_data_url(data_url: str, folder: str, prefix: str) -> str:
    """把前端 canvas 的 data:image/png;base64,... 存成檔案，回傳相對路徑。"""
    m = re.match(r"data:image/(png|jpeg);base64,(.+)", data_url or "", re.S)
    if not m:
        raise HTTPException(status_code=400, detail="簽名格式錯誤")
    ext = "png" if m.group(1) == "png" else "jpg"
    name = f"{prefix}-{uuid.uuid4().hex[:12]}.{ext}"
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(m.group(2)))
    return os.path.relpath(path, BASE_DIR).replace("\\", "/")


# ==========================================================================
# 認證
# ==========================================================================
@app.post("/api/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    request.session["user"] = {
        "id": user.id, "username": user.username, "name": user.display_name,
        "role": user.role, "site_id": user.site_id, "employee_no": user.employee_no,
    }
    return {"ok": True, "user": request.session["user"]}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"user": current_user(request)}


# ==========================================================================
# 主檔
# ==========================================================================
@app.get("/api/sites")
def list_sites(db: Session = Depends(get_db), user=Depends(need_login)):
    # 布林欄位一律用 == True；.is_(True) 在 SQL Server 會編譯成不合法的 `IS 1`
    return [{"id": s.id, "code": s.code, "name": s.name}
            for s in db.query(Site).filter(Site.active == True).all()]  # noqa: E712


@app.get("/api/vendors")
def list_vendors(db: Session = Depends(get_db), user=Depends(need_login)):
    return [{"id": v.id, "code": v.code, "name": v.name}
            for v in db.query(Vendor).filter(Vendor.active == True).all()]  # noqa: E712


@app.get("/api/forms")
def list_forms(db: Session = Depends(get_db), user=Depends(need_login)):
    rows = db.query(FormTemplate).filter(FormTemplate.active == True)\
        .order_by(FormTemplate.form_code).all()  # noqa: E712
    return [{"form_code": f.form_code, "title": f.title, "short_name": f.short_name,
             "form_type": f.form_type, "item_count": f.item_count} for f in rows]


@app.get("/api/forms/{form_code}")
def get_form(form_code: str, db: Session = Depends(get_db), user=Depends(need_login)):
    f = db.get(FormTemplate, form_code)
    if not f:
        raise HTTPException(404, "查無此表單")
    return {
        "form_code": f.form_code, "title": f.title, "form_type": f.form_type,
        "items": [{"id": i.id, "seq": i.seq, "category": i.category,
                   "hazard_code": i.hazard_code, "hazard_label": i.hazard_label,
                   "text": i.text} for i in f.items],
    }


# ==========================================================================
# 巡檢單
# ==========================================================================
@app.post("/api/inspections")
def create_inspection(payload: dict = Body(...), db: Session = Depends(get_db),
                      request: Request = None, user=Depends(need_login)):
    """建立並送出一張巡檢單。

    payload = {
      site_id, form_code, inspect_date, location, weather,
      results: [{item_id, result, remark, day?}],
      findings: [{item_id, description, hazard_code, vendor_id, responsible_person,
                  severity, action_type, due_date, photo_before, fix_note, photo_after}],
      signatures: [{role, signer_name, image}]   # image 為 data URL
    }
    """
    form = db.get(FormTemplate, payload.get("form_code"))
    if not form:
        raise HTTPException(400, "表單代碼錯誤")

    insp = Inspection(
        site_id=int(payload["site_id"]), form_code=form.form_code,
        inspect_date=date.fromisoformat(payload.get("inspect_date")
                                        or date.today().isoformat()),
        location=payload.get("location"), weather=payload.get("weather"),
        inspector_id=user["id"], status="submitted", submitted_at=datetime.now(),
    )
    db.add(insp)
    db.flush()

    for r in payload.get("results", []):
        db.add(InspectionResult(
            inspection_id=insp.id, item_id=int(r["item_id"]),
            day=r.get("day"), result=r.get("result", "na"), remark=r.get("remark"),
        ))

    created = []
    for f in payload.get("findings", []):
        item = db.get(FormItem, int(f["item_id"])) if f.get("item_id") else None
        due = f.get("due_date")
        action = f.get("action_type", "onsite")
        fd = Finding(
            site_id=insp.site_id, inspection_id=insp.id,
            item_id=item.id if item else None, source="inspection",
            found_at=datetime.now(), location=insp.location,
            hazard_code=f.get("hazard_code") or (item.hazard_code if item else "OTHER"),
            hazard_label=f.get("hazard_label") or (item.hazard_label if item else "其他"),
            description=f.get("description") or (item.text if item else ""),
            vendor_id=int(f["vendor_id"]) if f.get("vendor_id") else None,
            responsible_person=f.get("responsible_person"),
            severity=f.get("severity", "minor"), action_type=action,
            due_date=date.fromisoformat(due) if due and action == "scheduled" else None,
            photo_before=f.get("photo_before"), photo_after=f.get("photo_after"),
            created_by=user["id"],
        )
        if action == "onsite":
            fd.fixed_at = datetime.now()
            fd.fix_note = f.get("fix_note") or "當場改善完成"
            fd.status = "fixed"
        db.add(fd)
        created.append(fd)

    for s in payload.get("signatures", []):
        db.add(Signature(
            inspection_id=insp.id, role=s.get("role", "檢查人員"),
            signer_id=user["id"], signer_name=s.get("signer_name") or user["name"],
            image_path=_save_data_url(s.get("image"), SIG_DIR, "sig"),
            signed_ip=request.client.host if request and request.client else None,
        ))

    db.commit()
    db.refresh(insp)

    results = db.query(InspectionResult)\
        .filter(InspectionResult.inspection_id == insp.id)\
        .join(FormItem).order_by(FormItem.seq).all()
    sigs = db.query(Signature).filter(Signature.inspection_id == insp.id).all()
    insp.pdf_path = build_inspection_pdf(insp, results, created, sigs)
    db.commit()

    return {"ok": True, "inspection_id": insp.id, "pdf_url": f"/api/inspections/{insp.id}/pdf",
            "finding_ids": [f.id for f in created]}


@app.get("/api/inspections")
def list_inspections(site_id: int = None, days: int = 30,
                     db: Session = Depends(get_db), user=Depends(need_login)):
    q = db.query(Inspection).filter(
        Inspection.inspect_date >= date.today() - timedelta(days=days))
    if site_id:
        q = q.filter(Inspection.site_id == site_id)
    rows = q.order_by(Inspection.inspect_date.desc(), Inspection.id.desc()).limit(300).all()
    out = []
    for i in rows:
        fails = sum(1 for r in i.results if r.result == "fail")
        out.append({
            "id": i.id, "site": i.site.name, "site_id": i.site_id,
            "form_code": i.form_code, "form_title": i.form.title,
            "inspect_date": i.inspect_date.isoformat(), "location": i.location,
            "inspector": i.inspector.display_name, "status": i.status,
            "fail_count": fails, "item_count": len(i.results),
            "pdf_url": f"/api/inspections/{i.id}/pdf" if i.pdf_path else None,
        })
    return out


@app.get("/api/inspections/{insp_id}")
def get_inspection(insp_id: int, db: Session = Depends(get_db), user=Depends(need_login)):
    i = db.get(Inspection, insp_id)
    if not i:
        raise HTTPException(404, "查無此單")
    return {
        "id": i.id, "site": i.site.name, "form_title": i.form.title,
        "inspect_date": i.inspect_date.isoformat(), "location": i.location,
        "weather": i.weather, "inspector": i.inspector.display_name, "status": i.status,
        "results": [{"seq": r.item.seq, "category": r.item.category, "text": r.item.text,
                     "result": r.result, "remark": r.remark}
                    for r in sorted(i.results, key=lambda x: x.item.seq)],
        "findings": [{"id": f.id, "description": f.description,
                      "hazard_label": f.hazard_label, "status": f.status}
                     for f in i.findings],
        "pdf_url": f"/api/inspections/{i.id}/pdf" if i.pdf_path else None,
    }


@app.get("/api/inspections/{insp_id}/pdf")
def inspection_pdf(insp_id: int, db: Session = Depends(get_db), user=Depends(need_login)):
    i = db.get(Inspection, insp_id)
    if not i or not i.pdf_path:
        raise HTTPException(404, "PDF 尚未產生")
    path = os.path.join(BASE_DIR, i.pdf_path)
    if not os.path.exists(path):
        raise HTTPException(404, "PDF 檔案不存在")
    return FileResponse(path, media_type="application/pdf",
                        filename=os.path.basename(path))


# ==========================================================================
# 每日協議、巡視及處理紀錄表
# ==========================================================================
@app.post("/api/coordinations")
def create_coordination(payload: dict = Body(...), db: Session = Depends(get_db),
                        request: Request = None, user=Depends(need_login)):
    co = Coordination(
        site_id=int(payload["site_id"]),
        meeting_date=date.fromisoformat(payload.get("meeting_date")
                                        or date.today().isoformat()),
        work_date=date.fromisoformat(payload.get("work_date") or date.today().isoformat()),
        weather=payload.get("weather"),
        agreement_text=payload.get("agreement_text"),
        patrol_text=payload.get("patrol_text"),
        handling_text=payload.get("handling_text"),
        status="submitted", submitted_at=datetime.now(), created_by=user["id"],
    )
    db.add(co)
    db.flush()

    for a in payload.get("attendees", []):
        db.add(CoordinationAttendee(
            coordination_id=co.id, work_item=a.get("work_item"),
            vendor_id=int(a["vendor_id"]) if a.get("vendor_id") else None,
            vendor_name=a.get("vendor_name"), trade=a.get("trade"),
            person_name=a.get("person_name"), employee_no=a.get("employee_no"),
            work_content=a.get("work_content"),
        ))

    created = []
    for f in payload.get("findings", []):
        action = f.get("action_type", "onsite")
        due = f.get("due_date")
        fd = Finding(
            site_id=co.site_id, coordination_id=co.id,
            source="coordination", found_at=datetime.now(),
            location=f.get("location"), hazard_code=f.get("hazard_code", "OTHER"),
            hazard_label=f.get("hazard_label", "其他"), description=f["description"],
            vendor_id=int(f["vendor_id"]) if f.get("vendor_id") else None,
            responsible_person=f.get("responsible_person"),
            severity=f.get("severity", "minor"), action_type=action,
            due_date=date.fromisoformat(due) if due and action == "scheduled" else None,
            photo_before=f.get("photo_before"), photo_after=f.get("photo_after"),
            created_by=user["id"],
        )
        if action == "onsite":
            fd.fixed_at = datetime.now()
            fd.fix_note = f.get("fix_note") or "當場改善完成"
            fd.status = "fixed"
        db.add(fd)
        created.append(fd)

    for s in payload.get("signatures", []):
        db.add(Signature(
            coordination_id=co.id, role=s.get("role", "檢查人員"),
            signer_id=user["id"], signer_name=s.get("signer_name") or user["name"],
            image_path=_save_data_url(s.get("image"), SIG_DIR, "sig"),
            signed_ip=request.client.host if request and request.client else None,
        ))

    db.commit()
    db.refresh(co)
    sigs = db.query(Signature).filter(Signature.coordination_id == co.id).all()
    co.pdf_path = build_coordination_pdf(co, co.attendees, created, sigs)
    db.commit()
    return {"ok": True, "coordination_id": co.id,
            "pdf_url": f"/api/coordinations/{co.id}/pdf"}


@app.get("/api/coordinations")
def list_coordinations(days: int = 30, db: Session = Depends(get_db),
                       user=Depends(need_login)):
    rows = db.query(Coordination)\
        .filter(Coordination.work_date >= date.today() - timedelta(days=days))\
        .order_by(Coordination.work_date.desc()).limit(200).all()
    return [{"id": c.id, "site": c.site.name, "work_date": c.work_date.isoformat(),
             "attendee_count": len(c.attendees), "status": c.status,
             "pdf_url": f"/api/coordinations/{c.id}/pdf" if c.pdf_path else None}
            for c in rows]


@app.get("/api/coordinations/{co_id}/pdf")
def coordination_pdf(co_id: int, db: Session = Depends(get_db), user=Depends(need_login)):
    c = db.get(Coordination, co_id)
    if not c or not c.pdf_path:
        raise HTTPException(404, "PDF 尚未產生")
    path = os.path.join(BASE_DIR, c.pdf_path)
    if not os.path.exists(path):
        raise HTTPException(404, "PDF 檔案不存在")
    return FileResponse(path, media_type="application/pdf",
                        filename=os.path.basename(path))


# ==========================================================================
# 缺失
# ==========================================================================
@app.get("/api/findings")
def list_findings(site_id: int = None, status: str = None, overdue: bool = False,
                  days: int = 30, db: Session = Depends(get_db), user=Depends(need_login)):
    q = db.query(Finding).filter(
        Finding.found_at >= datetime.now() - timedelta(days=days))
    if site_id:
        q = q.filter(Finding.site_id == site_id)
    if status:
        q = q.filter(Finding.status == status)
    rows = q.order_by(Finding.found_at.desc()).limit(500).all()
    if overdue:
        rows = [f for f in rows if f.is_overdue]
    return [{
        "id": f.id, "no": f"F{f.id:06d}", "site": f.site.name, "site_id": f.site_id,
        "source": f.source, "found_at": f.found_at.isoformat(timespec="minutes"),
        "location": f.location, "hazard_code": f.hazard_code,
        "hazard_label": f.hazard_label, "description": f.description,
        "vendor": f.vendor.name if f.vendor else None,
        "responsible_person": f.responsible_person, "severity": f.severity,
        "action_type": f.action_type,
        "due_date": f.due_date.isoformat() if f.due_date else None,
        "status": f.status, "overdue": f.is_overdue,
        "photo_before": f.photo_before, "photo_after": f.photo_after,
    } for f in rows]


@app.post("/api/findings")
def create_finding(payload: dict = Body(...), db: Session = Depends(get_db),
                   user=Depends(need_login)):
    action = payload.get("action_type", "onsite")
    due = payload.get("due_date")
    f = Finding(
        site_id=int(payload["site_id"]), source=payload.get("source", "audit"),
        found_at=datetime.now(), location=payload.get("location"),
        hazard_code=payload.get("hazard_code", "OTHER"),
        hazard_label=payload.get("hazard_label", "其他"),
        description=payload["description"],
        vendor_id=int(payload["vendor_id"]) if payload.get("vendor_id") else None,
        responsible_person=payload.get("responsible_person"),
        severity=payload.get("severity", "minor"), action_type=action,
        due_date=date.fromisoformat(due) if due and action == "scheduled" else None,
        photo_before=payload.get("photo_before"), created_by=user["id"],
    )
    if action == "onsite":
        f.fixed_at = datetime.now()
        f.fix_note = payload.get("fix_note") or "當場改善完成"
        f.status = "fixed"
    db.add(f)
    db.commit()
    return {"ok": True, "finding_id": f.id}


@app.post("/api/findings/{fid}/fix")
def fix_finding(fid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                user=Depends(need_login)):
    f = db.get(Finding, fid)
    if not f:
        raise HTTPException(404, "查無此缺失")
    f.fixed_at = datetime.now()
    f.fix_note = payload.get("fix_note")
    f.photo_after = payload.get("photo_after") or f.photo_after
    f.status = "fixed"
    db.commit()
    return {"ok": True}


@app.post("/api/findings/{fid}/verify")
def verify_finding(fid: int, db: Session = Depends(get_db), user=Depends(need_login)):
    if user["role"] not in ("safety", "manager", "admin"):
        raise HTTPException(403, "僅職安人員或主管可複驗")
    f = db.get(Finding, fid)
    if not f:
        raise HTTPException(404, "查無此缺失")
    f.verifier_id = user["id"]
    f.verified_at = datetime.now()
    f.status = "closed"
    db.commit()
    return {"ok": True}


# ==========================================================================
# 檔案上傳
# ==========================================================================
@app.post("/api/upload/photo")
async def upload_photo(file: UploadFile = File(...), user=Depends(need_login)):
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "僅接受 jpg / png / webp")
    name = f"{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:12]}{ext}"
    path = os.path.join(PHOTO_DIR, name)
    with open(path, "wb") as f:
        f.write(await file.read())
    return {"ok": True, "path": os.path.relpath(path, BASE_DIR).replace("\\", "/")}


# ==========================================================================
# 戰情室儀表板
# ==========================================================================
@app.get("/api/dashboard")
def dashboard(site_id: int = None, days: int = 30, db: Session = Depends(get_db)):
    """儀表板彙總。戰情室大螢幕免登入即可讀取（唯讀，不含個資明細）。"""
    today = date.today()
    since = datetime.now() - timedelta(days=days)

    fq = db.query(Finding).filter(Finding.found_at >= since)
    if site_id:
        fq = fq.filter(Finding.site_id == site_id)
    findings = fq.all()

    todays = [f for f in findings if f.found_at.date() == today]
    open_items = [f for f in findings if f.status in ("open", "fixed")]
    overdue = [f for f in findings if f.is_overdue]

    by_hazard = {}
    for f in findings:
        by_hazard[f.hazard_label or "其他"] = by_hazard.get(f.hazard_label or "其他", 0) + 1

    by_vendor = {}
    for f in findings:
        if f.vendor:
            by_vendor[f.vendor.name] = by_vendor.get(f.vendor.name, 0) + 1

    trend = {}
    for i in range(13, -1, -1):
        trend[(today - timedelta(days=i)).isoformat()] = 0
    for f in findings:
        k = f.found_at.date().isoformat()
        if k in trend:
            trend[k] += 1

    iq = db.query(Inspection).filter(Inspection.inspect_date == today)
    if site_id:
        iq = iq.filter(Inspection.site_id == site_id)
    todays_insp = iq.all()

    site_rows = []
    for s in db.query(Site).filter(Site.active == True).all():  # noqa: E712
        s_find = [f for f in findings if f.site_id == s.id]
        s_over = [f for f in s_find if f.is_overdue]
        s_insp = len([i for i in todays_insp if i.site_id == s.id])
        site_rows.append({
            "site_id": s.id, "site": s.name,
            "findings": len(s_find), "open": len([f for f in s_find
                                                  if f.status in ("open", "fixed")]),
            "overdue": len(s_over), "inspections_today": s_insp,
            "light": "red" if s_over else ("yellow" if any(
                f.status == "open" for f in s_find) else "green"),
        })

    fixed_durations = [
        (f.fixed_at - f.found_at).total_seconds() / 3600
        for f in findings if f.fixed_at and f.found_at
    ]
    fixed_durations.sort()
    median_fix = (fixed_durations[len(fixed_durations) // 2]
                  if fixed_durations else None)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "range_days": days,
        "kpi": {
            "findings_today": len(todays),
            "findings_range": len(findings),
            "onsite_fixed": len([f for f in findings if f.action_type == "onsite"]),
            "scheduled": len([f for f in findings if f.action_type == "scheduled"]),
            "open": len(open_items),
            "overdue": len(overdue),
            "closed_rate": round(
                100 * len([f for f in findings if f.status == "closed"]) / len(findings), 1
            ) if findings else 100.0,
            "median_fix_hours": round(median_fix, 1) if median_fix is not None else None,
            "inspections_today": len(todays_insp),
        },
        "by_hazard": sorted([{"label": k, "count": v} for k, v in by_hazard.items()],
                            key=lambda x: -x["count"]),
        "by_vendor": sorted([{"label": k, "count": v} for k, v in by_vendor.items()],
                            key=lambda x: -x["count"])[:10],
        "trend": [{"date": k, "count": v} for k, v in trend.items()],
        "sites": site_rows,
        "overdue_list": sorted([{
            "no": f"F{f.id:06d}", "site": f.site.name, "description": f.description,
            "vendor": f.vendor.name if f.vendor else "", "person": f.responsible_person,
            "due_date": f.due_date.isoformat() if f.due_date else None,
            "days_over": (today - f.due_date).days if f.due_date else 0,
        } for f in overdue], key=lambda x: -x["days_over"])[:20],
        "recent": [{
            "no": f"F{f.id:06d}", "site": f.site.name,
            "found_at": f.found_at.isoformat(timespec="minutes"),
            "hazard_label": f.hazard_label, "description": f.description,
            "vendor": f.vendor.name if f.vendor else "", "status": f.status,
            "action_type": f.action_type,
        } for f in sorted(findings, key=lambda x: x.found_at, reverse=True)[:15]],
    }


# ==========================================================================
# 對外 API —— 設備廠商推送資料的統一入口
# ==========================================================================
@app.post("/api/v1/ingest/device")
def ingest_device(request: Request, payload: dict = Body(...),
                  db: Session = Depends(get_db)):
    """設備廠商（設備商甲／設備商乙／設備商丙…）依此格式推送資料。

    Header: X-Vendor-Token: <廠商權杖>
    Body:
    {
      "vendor_code": "vendor-a",
      "site_code": "SITE-A",
      "device_type": "access",           # access / env / cctv_ai
      "device_id": "GATE-01",
      "readings": [
        {"metric": "headcount_in", "value_num": 128,
         "reading_at": "2026-08-17T09:00:00"},
        {"metric": "alarm", "value_text": "未戴安全帽",
         "reading_at": "2026-08-17T09:03:12"}
      ]
    }
    """
    token = request.headers.get("X-Vendor-Token", "")
    vendor_code = payload.get("vendor_code", "")
    if INGEST_TOKENS.get(vendor_code) != token:
        raise HTTPException(401, "廠商權杖驗證失敗")

    site = db.query(Site).filter(Site.code == payload.get("site_code")).first()
    n = 0
    for r in payload.get("readings", []):
        db.add(DeviceReading(
            site_id=site.id if site else None, site_code=payload.get("site_code"),
            vendor_code=vendor_code, device_type=payload.get("device_type"),
            device_id=payload.get("device_id"), metric=r.get("metric"),
            value_num=r.get("value_num"), value_text=r.get("value_text"),
            reading_at=datetime.fromisoformat(r["reading_at"]),
            raw_payload=str(r),
        ))
        n += 1
    db.commit()
    return {"ok": True, "accepted": n}


@app.get("/api/v1/device/latest")
def device_latest(site_code: str = None, device_type: str = None, limit: int = 50,
                  db: Session = Depends(get_db)):
    q = db.query(DeviceReading)
    if site_code:
        q = q.filter(DeviceReading.site_code == site_code)
    if device_type:
        q = q.filter(DeviceReading.device_type == device_type)
    rows = q.order_by(DeviceReading.reading_at.desc()).limit(limit).all()
    return [{"site_code": r.site_code, "vendor_code": r.vendor_code,
             "device_type": r.device_type, "device_id": r.device_id,
             "metric": r.metric, "value_num": float(r.value_num) if r.value_num else None,
             "value_text": r.value_text,
             "reading_at": r.reading_at.isoformat(timespec="seconds")} for r in rows]


# ==========================================================================
# 靜態網頁
# ==========================================================================
@app.get("/")
def root():
    return RedirectResponse("/static/index.html")


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True),
          name="static")


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    """健康檢查：確認資料庫連得上、模板已匯入。"""
    return {
        "ok": True,
        "database": db_info(),
        "form_templates": db.query(FormTemplate).count(),
        "form_items": db.query(FormItem).count(),
        "findings": db.query(Finding).count(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


@app.on_event("startup")
def _startup():
    init_db()
    print(f"[safety-ops] 資料庫：{db_info()}")
