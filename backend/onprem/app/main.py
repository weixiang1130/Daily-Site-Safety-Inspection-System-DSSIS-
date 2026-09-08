# -*- coding: utf-8 -*-
"""職安填報系統 API。

啟動：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import base64
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

# 必須在下面任何 os.environ.get() 之前載入，否則設定檔填了也不會生效
from .envfile import load_env

load_env()

from .auth import SECRET_KEY, authenticate, current_user  # noqa: E402
from .hazard import (LEVEL_LABEL, level_of, station_level,  # noqa: E402
                     heat_index_c, thresholds_payload)
from .heat_guidance import heat_guidance
from .noise_guidance import noise_guidance, period_alarms
from .work_hazards import hazards_of
from . import cctv
from .db import (
    BASE_DIR, Coordination, CoordinationAttendee, DeviceReading, Finding, FormItem,
    FormTemplate, Inspection, InspectionResult, NewsItem, PlannedTask, SessionLocal,
    Signature, Site, User,
    Vendor, WorkLog, db_info, init_db,
)
from .pdf import build_coordination_pdf, build_inspection_pdf
from .site_board import board_payload, validate_config

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
PHOTO_DIR = os.path.join(UPLOAD_DIR, "photos")
SIG_DIR = os.path.join(UPLOAD_DIR, "signatures")
for d in (PHOTO_DIR, SIG_DIR):
    os.makedirs(d, exist_ok=True)

def _env_pairs(name: str) -> dict:
    """逗號分隔的 key:value 環境變數 → dict。

    INGEST_TOKENS 與 BUILDING_LABELS 共用同一條解析規則。用 partition
    而不是 split(":")[1]——值裡含冒號時後者會把值截斷，權杖被截斷等於
    驗證永遠對不上，而且錯誤完全無聲。
    """
    out = {}
    for pair in os.environ.get(name, "").split(","):
        key, _, value = pair.partition(":")
        if key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


# 設備廠商推送資料用的權杖，一家廠商一組（格式 vendor-a:xxx,vendor-b:yyy）。
# 刻意沒有預設值：預設權杖印在公開 repo 裡，等於任何人都能推送偽造的
# 環境數據與人數——而疏散點名會照著牆上的數字。未設定時端點直接停用。
INGEST_TOKENS = _env_pairs("INGEST_TOKENS")

# site_code → 棟別標籤（如 BD04:辦公棟,BD05:住宅棟）。
# 主場站的兩張建照各自匯入列控表（site_code 不同），牆上的重點工項與
# 進度卡片用這個對應顯示棟別。env 只在啟動時載入（load_env），
# 因此在模組層解析一次即可，不必每個請求重算。
BUILDING_LABELS = _env_pairs("BUILDING_LABELS")

# 品牌識別。本 repo 為公開，預設值一律為中性名稱；
# 實際公司名稱由部署環境的環境變數提供，不寫進程式碼。
BRANDING = {
    "system_name": os.environ.get("SYSTEM_NAME", "職安填報系統"),
    "war_room_name": os.environ.get("WAR_ROOM_NAME", "職安戰情室"),
    "org_name": os.environ.get("BRAND_NAME", "示範營造股份有限公司"),
    "org_short": os.environ.get("BRAND_SHORT_NAME", "示範營造"),
    "org_name_en": os.environ.get("BRAND_NAME_EN", "Demo Construction"),
    "group_name": os.environ.get("BRAND_GROUP", ""),
    # 戰情室的主場站。環境與進出場人次以它為主，缺失統計仍涵蓋全部工地；
    # 填報頁（前端）據此只列主場站。strip：這個值到處被拿去做字串比對，
    # 設定檔帶到尾隨空白就是「一個工地」與「四十個工地」的差別。
    "primary_site_code": os.environ.get("PRIMARY_SITE_CODE", "").strip(),
    # 棟別選項：BUILDING_LABELS 的標籤值依序去重。前端填報選單據此建立，
    # 未設定時退回預設清單（frontend/common.js 的 BUILDINGS）。
    "buildings": list(dict.fromkeys(BUILDING_LABELS.values())),
    # 戰情室就跑在這裡，首頁要顯示入口。
    "war_room": True,
    # 這一側有即時的 /api/dashboard，前端走即時查詢而不是讀快照。
    # 雲端那側為 True（只有地端推上去的快照可讀）。
    "wallboard": False,
}

# 戰情室大螢幕是否免登入。放在公司內網時可設為 true（大螢幕不必有人登入）；
# **部署到公開網際網路時務必維持 false**，否則任何拿到網址的人都能看到
# 全公司的缺失、廠商與工地資料。
PUBLIC_DASHBOARD = os.environ.get("PUBLIC_DASHBOARD", "false").lower() == "true"

# 部署在 HTTPS 後方時，session cookie 應限定僅走加密連線。
HTTPS_ONLY = os.environ.get("HTTPS_ONLY", "false").lower() == "true"

app = FastAPI(title=BRANDING["system_name"], version="0.1.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=12 * 3600,
    same_site="lax",
    https_only=HTTPS_ONLY,
)


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


@app.get("/api/board-sites")
def board_sites(request: Request, db: Session = Depends(get_db)):
    if not PUBLIC_DASHBOARD:
        need_login(request)
    return [{"id": s.id, "code": s.code, "name": s.name}
            for s in db.query(Site).filter(Site.active == True).order_by(Site.sort_order, Site.id)]


@app.get("/api/site-board/{site_id}")
def read_site_board(site_id: int, request: Request, db: Session = Depends(get_db)):
    if not PUBLIC_DASHBOARD:
        need_login(request)
    site = db.get(Site, site_id)
    if not site or not site.active:
        raise HTTPException(404, "工地不存在或已停用")
    result = board_payload(site)
    result["management_url"] = os.environ.get("CLOUD_API_URL", "").rstrip("/")
    return result


@app.post("/api/site-board/{site_id}")
def save_site_board(site_id: int, request: Request, payload: dict = Body(...),
                    db: Session = Depends(get_db)):
    session = need_login(request)
    user = db.get(User, session["id"])
    if not user or not user.active or not (user.role == "admin" or
            (user.role in ("safety", "manager") and user.site_id == site_id)):
        raise HTTPException(403, "僅管理員或所屬工地的職安人員、主管可維護")
    if os.environ.get("CLOUD_API_URL", "").strip():
        raise HTTPException(409, "請至雲端填報站維護，設定將隨表單同步帶回")
    site = db.get(Site, site_id)
    if not site or not site.active:
        raise HTTPException(404, "工地不存在或已停用")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0:
        raise HTTPException(400, "設定版本格式錯誤")
    try:
        config = validate_config(payload.get("config"))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    config["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    changed = db.query(Site).filter(Site.id == site_id,
        func.coalesce(Site.board_revision, 0) == revision).update({
        Site.board_config: json.dumps(config, ensure_ascii=False),
        Site.board_revision: revision + 1}, synchronize_session=False)
    if not changed:
        db.rollback()
        raise HTTPException(409, "其他人已更新，請重新載入再編輯")
    db.commit()
    db.refresh(site)
    return board_payload(site)


@app.get("/api/board-data/{site_id}")
def board_data(site_id: int, request: Request, db: Session = Depends(get_db)):
    """工地看板的整頁資料。

    看板每分鐘輪詢一次，一次呼叫拿齊五個區塊（環境、缺失統計、出工、
    無災害工時、職安新知），不讓牆面打五支 API。
    """
    if not PUBLIC_DASHBOARD:
        need_login(request)
    site = db.get(Site, site_id)
    if not site or not site.active:
        raise HTTPException(404, "工地不存在或已停用")
    today = date.today()

    # 環境：看板固定顯示主場站的測站；主場站對不到就退而取危害最高的一站
    env_all = environment_snapshot(db)
    primary_code = BRANDING["primary_site_code"]
    station = next((s for s in env_all if s["site_code"] == primary_code),
                   env_all[0] if env_all else None)

    # 缺失統計：與首頁 /api/findings/summary 同一份、同一口徑（不分工地、
    # 不設時間窗）——同一間工務所兩個螢幕不能對同一個標籤給兩種數字
    stats = finding_stats(db)

    # 本日出工（工務所 LINE 出工回報，collectors/worklog.py 解析落地）
    wl = (db.query(WorkLog).filter(WorkLog.report_date == today)
          .order_by(WorkLog.building, WorkLog.vendor).all())
    worklog = {
        "date": today.isoformat(),
        "total": sum(w.headcount or 0 for w in wl),
        "rows": [{
            "building": w.building, "vendor": w.vendor, "trade": w.trade,
            "headcount": w.headcount, "supervisor": w.supervisor,
            "tasks": w.tasks,
            "reported_at": (w.reported_at.isoformat(timespec="minutes")
                            if w.reported_at else None),
        } for w in wl],
    }

    # 今日作業危害告知：出工回報的工種與施作項目＋列控表今日進行中的
    # 葉工項，經 work_hazards 對應出「應注意危害」。只做提示不做判定
    # （對不到關鍵字的作業不硬湊），佈告輪播會插一張告知卡。
    hazard_map = {}          # 危害 → 觸發它的作業來源（去重、保序）

    def note_hazards(text, source):
        for hz in hazards_of(text or ""):
            lst = hazard_map.setdefault(hz, [])
            if source not in lst and len(lst) < 6:
                lst.append(source)

    for w in wl:
        label = w.vendor + (f"（{w.building}）" if w.building else "")
        note_hazards(f"{w.trade or ''} {w.tasks or ''}", label)
    codes = list(BUILDING_LABELS) or ([primary_code] if primary_code else [])
    if codes:
        for t in (db.query(PlannedTask)
                  .filter(PlannedTask.is_leaf == True,  # noqa: E712
                          PlannedTask.site_code.in_(codes),
                          PlannedTask.start_date <= today,
                          PlannedTask.end_date >= today).all()):
            bld = BUILDING_LABELS.get(t.site_code)
            note_hazards(t.name, t.name[:16] + (f"（{bld}）" if bld else ""))
    hazards = sorted(
        [{"label": k, "sources": v} for k, v in hazard_map.items()],
        key=lambda x: -len(x["sources"]))

    # 無災害紀錄以「天」計：起算日當天算第 1 天，逐日累計；起算日
    # （出過事就改成復工日重算）與起算前已累計天數由看板維護頁提供。
    # 未設定（或還沒到）起算日就回 None、牆上顯示「—」並提示設定——
    # 這是訪客與稽核會當真的數字，用別的日期湊出來的值比空白更糟，
    # 而且會跨過沒登錄的事故繼續累加。
    board = board_payload(site)
    safety = board["config"].get("safety") or {}
    base_days = int(safety.get("base_days") or 0)
    start_raw = safety.get("start_date") or ""
    days = None
    if start_raw:
        start_d = date.fromisoformat(start_raw)
        if start_d <= today:
            days = base_days + (today - start_d).days + 1
    # 上月總出工（人日）：出工回報人數逐日加總
    first_this = today.replace(day=1)
    lm_end = first_this - timedelta(days=1)
    lm = (db.query(func.coalesce(func.sum(WorkLog.headcount), 0))
          .filter(WorkLog.report_date >= lm_end.replace(day=1),
                  WorkLog.report_date <= lm_end).scalar())
    record = {
        "days": days,
        "last_month_mandays": int(lm or 0),
        "since": start_raw or None,
    }

    news = [{"title": n.title, "url": n.url,
             "published": n.published.isoformat() if n.published else None}
            for n in db.query(NewsItem)
            .order_by(NewsItem.published.desc(), NewsItem.id.desc())
            .limit(8).all()]

    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "station": station, "stats": stats, "worklog": worklog,
            "record": record, "hazards": hazards, "news": news, "board": board}


def clean_building(payload: dict):
    """棟別欄位淨化。資料庫此欄為 32 字（NVARCHAR(32)），超長值會讓寫入
    失敗——雲端同步側甚至會因一筆髒值整批 rollback、每輪重試同一批。
    表單送不出超長值，這裡擋的是直接打 API 的寫入。"""
    v = (payload.get("building") or "").strip()
    if len(v) > 32:
        raise HTTPException(400, "棟別長度不可超過 32 字")
    return v or None


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
    #
    # 一律回傳完整清單。「填報只列主場站」是填報頁自己的事
    # （frontend/common.js 的 fillableSites）——這個端點同時供缺失清單／
    # 儀表板的瀏覽篩選與匯入工具查工地主檔，在這裡過濾會讓那些消費者
    # 拿到殘缺的主檔（實測害補登工具跟 e2e 測試直接掛）。
    return [{"id": s.id, "code": s.code, "name": s.name}
            for s in db.query(Site).filter(Site.active == True).all()]  # noqa: E712


@app.get("/api/vendors")
def list_vendors(db: Session = Depends(get_db), user=Depends(need_login)):
    return [{"id": v.id, "code": v.code, "name": v.name}
            for v in db.query(Vendor).filter(Vendor.active == True)  # noqa: E712
            .order_by(Vendor.name).all()]


@app.post("/api/vendors/resolve")
def resolve_vendor(payload: dict = Body(...), db: Session = Depends(get_db),
                   user=Depends(need_login)):
    """依名稱取得廠商，不存在就建立。

    協力商在各工地差異很大且會隨工程階段更換，不可能由管理員預先維護齊全，
    因此填報時允許現場直接輸入新廠商名稱。這裡必須建立正式的廠商資料
    （而非存成自由文字），否則儀表板的「廠商缺失排行」會漏統計。
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "廠商名稱不可為空")
    if len(name) > 128:
        raise HTTPException(400, "廠商名稱過長")

    # 比對時忽略大小寫與前後空白，避免同一家廠商因輸入差異被建成兩筆
    existing = next(
        (v for v in db.query(Vendor).all() if v.name.strip().lower() == name.lower()),
        None)
    if existing:
        if not existing.active:
            existing.active = True
            db.commit()
        return {"id": existing.id, "code": existing.code, "name": existing.name,
                "created": False}

    v = Vendor(code=f"TMP-{uuid.uuid4().hex[:12]}", name=name, active=True)
    db.add(v)
    db.commit()
    v.code = f"V{v.id:03d}"
    db.commit()
    return {"id": v.id, "code": v.code, "name": v.name, "created": True}


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
        site_id=int(payload["site_id"]),
        building=clean_building(payload),
        form_code=form.form_code,
        inspect_date=date.fromisoformat(payload.get("inspect_date")
                                        or date.today().isoformat()),
        location=payload.get("location"), weather=payload.get("weather"),
        inspector_id=user["id"],
        # 共用帳號下，帳號名稱代表不了實際檢查人，因此以填報時填寫的姓名為準
        inspector_name=(payload.get("inspector_name") or "").strip() or None,
        status="submitted", submitted_at=datetime.now(),
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
            # 缺失沿用表單的棟別：缺失是在填這張表時發現的，棟別必然相同
            site_id=insp.site_id, building=insp.building, inspection_id=insp.id,
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
            "building": i.building,
            "form_code": i.form_code, "form_title": i.form.title,
            "inspect_date": i.inspect_date.isoformat(), "location": i.location,
            "inspector": i.inspector_name or i.inspector.display_name, "status": i.status,
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
        "weather": i.weather, "inspector": i.inspector_name or i.inspector.display_name, "status": i.status,
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
        building=clean_building(payload),
        meeting_date=date.fromisoformat(payload.get("meeting_date")
                                        or date.today().isoformat()),
        work_date=date.fromisoformat(payload.get("work_date") or date.today().isoformat()),
        weather=payload.get("weather"),
        agreement_text=payload.get("agreement_text"),
        patrol_text=payload.get("patrol_text"),
        handling_text=payload.get("handling_text"),
        recorder_name=(payload.get("recorder_name") or "").strip() or None,
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
            site_id=co.site_id, building=co.building, coordination_id=co.id,
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
    return [{"id": c.id, "site": c.site.name, "building": c.building,
             "work_date": c.work_date.isoformat(),
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
        "building": f.building,
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
        site_id=int(payload["site_id"]), building=clean_building(payload),
        source=payload.get("source", "audit"),
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
    # 回傳可直接放進 href 的絕對路徑，與 Netlify 版的回傳格式一致
    return {"ok": True, "path": "/" + os.path.relpath(path, BASE_DIR).replace("\\", "/")}


# ==========================================================================
# 戰情室儀表板
# ==========================================================================
# ---------------------------------------------------------------------------
# 監視器
#
# 地端伺服器就在公司網路內，直接向 NVR 取像即可；雲端那套推送機制
# （backend/tools/push_snapshots.py + 儲存區 + 權杖）在這裡都不需要。
# ---------------------------------------------------------------------------

@app.get("/api/cctv/channels")
def cctv_channels(request: Request):
    """有哪些頻道可看。未設定 CAM_* 時回空清單，前端據此隱藏整格。"""
    if not PUBLIC_DASHBOARD and not current_user(request):
        raise HTTPException(status_code=401, detail="請先登入")
    return {"enabled": cctv.enabled(), "channels": cctv.channels()}


@app.get("/api/cctv/snapshot")
def cctv_snapshot(request: Request, channel: int = 0):
    """取一張畫面。

    路徑與參數刻意與雲端版相同，前端才能同一份程式碼兩邊都跑得動——
    戰情室搬到地端的期間，兩邊會並存一陣子。
    """
    if not PUBLIC_DASHBOARD and not current_user(request):
        raise HTTPException(status_code=401, detail="請先登入")
    try:
        data = cctv.snapshot(channel)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:                              # noqa: BLE001
        # 監視器離線不該讓整個牆面看起來像壞掉，回 503 讓前端只在那一格
        # 顯示訊息。例外原文只進伺服器記錄——它可能含內部主機位址，
        # 不該回給未登入就看得到的牆面。
        print(f"[cctv] 頻道 {channel} 取像失敗：{e}")
        raise HTTPException(status_code=503,
                            detail="取像失敗，請查看伺服器記錄")

    return Response(
        content=data, media_type="image/jpeg",
        # 已經在伺服器端做了短暫快取，瀏覽器再自行快取會讓畫面停住不動
        headers={"Cache-Control": "no-store"},
    )


# 雲端同步用的佔位帳號。它只是為了滿足 inspector_id 的外鍵限制而存在，
# 不是真的有這個人，因此絕不可出現在牆上——顯示成「雲端同步 填了這張表」
# 會讓看板的人以為那是檢查人員的姓名。
SYNC_PLACEHOLDER_USER = "cloud-sync"


def _person_of(inspection) -> str:
    """檢查人員姓名。取不到就留白，不要退回帳號名稱。

    現場共用同一組帳號登入，帳號的顯示名稱答不出「這張表是誰檢查的」；
    早期的紀錄沒有 inspector_name 欄位，補不出來就該留白，
    填一個看起來像姓名的東西比空白更糟。
    """
    if inspection.inspector_name:
        return inspection.inspector_name
    u = inspection.inspector
    if u and u.username != SYNC_PLACEHOLDER_USER:
        return u.display_name or ""
    return ""


def finding_stats(db: Session) -> dict:
    """今日新增／未結案／逾期三個彙總數字，COUNT 聚合、不撈整表。

    首頁與工地看板共用同一份，同一個標籤在兩個螢幕上才不會是兩種數字。
    刻意不設時間窗：「逾期未改善」放越久越該被看到，設了 30 天窗
    反而讓拖最久的那筆先從牆上消失。
    逾期條件須與 Finding.is_overdue 一致（限期改善、有期限、未複驗
    結案、期限已過）——改其中一邊時另一邊要跟上。
    """
    today = date.today()
    day_start = datetime(today.year, today.month, today.day)
    count = func.count(Finding.id)
    return {
        "findings_today": db.query(count)
            .filter(Finding.found_at >= day_start).scalar() or 0,
        "open": db.query(count)
            .filter(Finding.status.in_(("open", "fixed"))).scalar() or 0,
        "overdue": db.query(count)
            .filter(Finding.action_type == "scheduled",
                    Finding.due_date.isnot(None),
                    ~Finding.status.in_(("verified", "closed")),
                    Finding.due_date < today).scalar() or 0,
    }


@app.get("/api/findings/summary")
def findings_summary(db: Session = Depends(get_db), user=Depends(need_login)):
    """首頁「缺失概況」的三個數字。

    與雲端同名同結構：首頁兩邊共用同一份程式碼，端點不一致就得在前端分岔。
    刻意不重用 /api/dashboard——那份彙總很重，首頁只要三個數字。
    """
    return finding_stats(db)


def environment_snapshot(db: Session, site_by_id: dict = None,
                         site_by_code: dict = None) -> list:
    """各測站的環境即時值與危害判定。儀表板與工地看板共用。

    每個測站每個指標取三小時內最新的一筆，危害等級高的排前面。
    """
    if site_by_id is None or site_by_code is None:
        _all_sites = db.query(Site).all()
        site_by_id = {s.id: s for s in _all_sites}
        site_by_code = {s.code: s for s in _all_sites if s.code}

    env_since = datetime.now() - timedelta(hours=3)
    env_rows = (db.query(DeviceReading)
                .filter(DeviceReading.device_type == "env",
                        DeviceReading.reading_at >= env_since)
                .order_by(DeviceReading.reading_at.desc()).all())

    stations = {}
    for r in env_rows:
        st = stations.setdefault(r.device_id, {
            "device_id": r.device_id, "site": None, "site_code": r.site_code,
            "station": r.device_id, "reading_at": None, "metrics": {},
        })
        if r.metric in st["metrics"]:
            continue                      # 已取到更新的一筆
        st["metrics"][r.metric] = float(r.value_num) if r.value_num is not None else None
        stamp = r.reading_at.isoformat(timespec="minutes")
        if st["reading_at"] is None or stamp > st["reading_at"]:
            st["reading_at"] = stamp
        if st["site"] is None:
            # 先用 site_id，沒有就退而用 site_code 對。收集程式若在工地資料
            # 同步進來之前就跑過，那批讀值的 site_id 會是空的，只認 site_id
            # 會讓牆上顯示一串 MAC 位址而不是工地名稱。
            site_obj = site_by_id.get(r.site_id) if r.site_id else None
            if site_obj is None and r.site_code:
                site_obj = site_by_code.get(r.site_code)
            if site_obj:
                st["site"] = site_obj.name
                st["site_code"] = site_obj.code

    environment = []
    for st in stations.values():
        # 廠商的危害等級不可信（實測熱指數 49.4 仍回報 0），排除在判定之外。
        # 噪音時段警報也排除：那是環保的營建工程周界噪音管制，與勞工聽力
        # 保護是兩回事，併進危害等級會讓現場以為「環保沒超標＝聽力沒問題」。
        judged = {k: v for k, v in st["metrics"].items()
                  if k not in ("hazard_level", "vendor_hazard_level")
                  and not k.startswith("noise_alarm")}
        hi = judged.get("heat_index")
        if hi is None and judged.get("temperature") is not None \
                and judged.get("humidity") is not None:
            derived = heat_index_c(judged["temperature"], judged["humidity"])
            if derived is not None:
                # 推導值要寫回 metrics 再做分級：牆上的警示卡與數值表都讀
                # metrics，只留在 guidance 裡會出現「第三級卻顯示 — °C」，
                # 而且測站級的危害判定也會漏掉這個推導出來的熱指數
                hi = round(float(derived), 1)
                judged["heat_index"] = hi
                st["metrics"]["heat_index"] = hi

        st["levels"] = {k: level_of(k, v) for k, v in judged.items()}
        st["level"] = station_level(judged)
        st["level_label"] = LEVEL_LABEL[st["level"]]
        st["heat"] = heat_guidance(hi)

        # 噪音：職安的聽力保護（依即時音壓級）與環保的時段管制（廠商旗標）
        # 分開呈現，兩者法源與主管機關都不同
        st["noise"] = noise_guidance(judged.get("noise"))
        st["noise_alarm"] = period_alarms(st["metrics"])

        environment.append(st)

    # 危害等級高的排前面，值班人員第一眼就看到最需要處理的工地
    environment.sort(key=lambda x: -x["level"])
    return environment


@app.get("/api/dashboard")
def dashboard(request: Request, site_id: int = None, days: int = 30,
              db: Session = Depends(get_db)):
    """儀表板彙總。

    預設需登入。設定 PUBLIC_DASHBOARD=true 可開放免登入，
    僅建議在公司內網的戰情室大螢幕使用。
    """
    if not PUBLIC_DASHBOARD and not current_user(request):
        raise HTTPException(status_code=401, detail="請先登入")
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

    # 只計已送出的。草稿是還在填的表，把它算進「今日檢查」會虛增數字，
    # 而這個數字是長官在牆上直接看的。
    iq = (db.query(Inspection)
          .filter(Inspection.inspect_date == today, Inspection.status != "draft"))
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

    # ------------------------------------------------------------------
    # 現場即時：環境、人數、最新表單
    #
    # 這幾塊是戰情室搬到地端的主因——資料源都在公司網路內，雲端根本連不到
    # 監視器，也沒必要讓每張影像來回穿越網際網路。
    # ------------------------------------------------------------------
    primary_code = BRANDING["primary_site_code"]
    # 牆上重點工項與進度卡片的棟別標籤，模組層解析一次（見 BUILDING_LABELS）
    bld_labels = BUILDING_LABELS

    _all_sites = db.query(Site).all()
    site_by_id = {s.id: s for s in _all_sites}
    site_by_code = {s.code: s for s in _all_sites if s.code}

    environment = environment_snapshot(db, site_by_id, site_by_code)

    # 人數：一小時內最新一筆。固定以主場站為準，解析不到就不顯示——
    # 退回全公司加總會把總和放在單一工地名稱底下，疏散時會照著錯的數字點名。
    head_since = datetime.now() - timedelta(hours=1)
    head_rows = (db.query(DeviceReading)
                 .filter(DeviceReading.device_type == "people",
                         DeviceReading.reading_at >= head_since)
                 .order_by(DeviceReading.reading_at.desc()).all())

    headcount = {}
    seen_metrics = set()
    for r in head_rows:
        if primary_code and r.site_code != primary_code:
            continue
        if r.metric in seen_metrics:
            continue
        seen_metrics.add(r.metric)
        headcount[r.metric] = float(r.value_num) if r.value_num is not None else None
        headcount["reading_at"] = r.reading_at.isoformat(timespec="minutes")

    # 最新交出來的表單：工地填完後要在牆上馬上看得到結果
    recent_insp = (db.query(Inspection)
                   .filter(Inspection.status != "draft")
                   .order_by(Inspection.inspect_date.desc(), Inspection.id.desc())
                   .limit(20).all())
    recent_coord = (db.query(Coordination)
                    .filter(Coordination.status != "draft")
                    .order_by(Coordination.work_date.desc(), Coordination.id.desc())
                    .limit(20).all())

    recent_forms = []
    for i in recent_insp:
        # 本機填的表有逐項結果就直接算；雲端同步來的沒有逐項資料，用同步帶回來的
        # 彙總。兩者都沒有才是真的未知——這時不能顯示「全數符合」，那會讓牆上把
        # 一張有缺失的表看成沒問題。
        if i.results:
            fails = len([r for r in i.results if r.result == "fail"])
        else:
            fails = i.fail_count
        recent_forms.append({
            "kind": "inspection", "on_date": i.inspect_date.isoformat(),
            "site": i.site.name if i.site else "", "building": i.building,
            "title": i.form.title if getattr(i, "form", None) else i.form_code,
            "person": _person_of(i),
            "result": ("未知" if fails is None else
                       f"{fails} 項不符合" if fails else "全數符合"),
            "ok": fails == 0,
            "pdf_url": f"/api/inspections/{i.id}/pdf" if i.pdf_path else None,
        })
    for c in recent_coord:
        recent_forms.append({
            "kind": "coordination", "on_date": c.work_date.isoformat(),
            "site": c.site.name if c.site else "", "building": c.building,
            "title": "每日協議、巡視及處理紀錄表",
            "person": c.recorder_name or "",
            "result": f"{len(c.attendees) or c.attendee_count or 0} 家廠商",
            "ok": True,
            "pdf_url": f"/api/coordinations/{c.id}/pdf" if c.pdf_path else None,
        })
    recent_forms.sort(key=lambda x: x["on_date"], reverse=True)
    recent_forms = recent_forms[:20]

    # ------------------------------------------------------------------
    # 今日重點工項
    #
    # 回答牆上的第二個問題：「今天在做什麼、要注意什麼」。
    # 資料來自列控表匯入（backend/tools/import_schedule.py）；未來現場改為每日
    # 回報實際進度時，同一張表以 source='daily_report' 並存即可。
    # 只取葉工項——上層的「基礎工程」是彙總，掛上牆沒有意義。
    # ------------------------------------------------------------------
    tt = date.today()
    # 一次抓回全部葉工項：「今日進行中」在 Python 內過濾，「預定進度」
    # （下方）用同一份清單加總。原本各查一次，第二次還是無日期條件的
    # 全表掃描——這個端點每分鐘被牆面與每台檢視器各打一次，省一趟是一趟。
    leaf_tasks = (db.query(PlannedTask)
                  .filter(PlannedTask.is_leaf == True)  # noqa: E712
                  .order_by(PlannedTask.site_code, PlannedTask.start_date)
                  .all())
    ptasks = [t for t in leaf_tasks if t.start_date <= tt <= t.end_date]
    today_tasks = []
    for t in ptasks:
        t_site = site_by_id.get(t.site_id)
        total = (t.end_date - t.start_date).days + 1
        today_tasks.append({
            "site": t_site.name if t_site else t.site_code,
            "site_code": t.site_code,
            "building": bld_labels.get(t.site_code),
            "name": t.name,
            "start": t.start_date.isoformat(),
            "end": t.end_date.isoformat(),
            "day_no": (tt - t.start_date).days + 1,
            "total_days": total,
            # 首日與末日要特別標示：吊裝首日、灌漿收尾都是事故高發時點
            "first_day": t.start_date == tt,
            "last_day": t.end_date == tt,
            "hazards": hazards_of(t.name),
        })

    # ------------------------------------------------------------------
    # 工程進度（財務）
    #
    # 來自 FinOps 月結（collectors/finops.py）。**以金額衡量**，不是施工
    # 進度——牆上必須標明，否則會被當成現場做到幾成。
    # 對照的是「實際 vs 預估」：成本投入是 S 曲線，拿時間進度比會誤判。
    # ------------------------------------------------------------------
    prog_rows = (db.query(DeviceReading)
                 .filter(DeviceReading.device_type == "progress")
                 .order_by(DeviceReading.reading_at.desc()).all())
    prog_by_site = {}
    for r in prog_rows:
        sc = r.site_code or r.device_id
        slot = prog_by_site.setdefault(sc, {"site_code": sc, "at": r.reading_at})
        if slot["at"] != r.reading_at:
            continue              # 只取每個工地最新一個月結
        slot[r.metric] = float(r.value_num) if r.value_num is not None else None

    progress = []
    for sc, slot in prog_by_site.items():
        actual, est = slot.get("progress_actual"), slot.get("progress_est")
        if actual is None or est is None:
            continue
        p_site = site_by_code.get(sc)
        at = slot["at"]
        progress.append({
            "site": p_site.name if p_site else sc,
            "site_code": sc,
            "building": bld_labels.get(sc),
            "source": "finops",
            "actual": actual, "est": est,
            "gap": round(actual - est, 1),      # 正＝超前、負＝落後
            "time_rate": slot.get("progress_time"),
            # 公司慣用民國年月；月結成本落後當期約一個月，標明截止月份
            "month": f"{at.year - 1911}/{at.month:02d}",
        })

    # ------------------------------------------------------------------
    # 沒有 FinOps 月結的部署（工地檢視器完全不碰公司資料庫）退而求其次：
    # 依列控表工期推算「預定進度」——每個葉工項算已過工期，工期加權加總。
    #
    # 這與 FinOps 的意義完全不同：工期加權而非產值加權（與全程營運的
    # S 曲線會有出入），而且**沒有實際值可對照、看不出超前落後**。
    # 前端據 source 欄位分開呈現並標明推算依據，不與實際進度混用。
    # 已有 FinOps 資料的工地不覆蓋——實際值永遠優先。
    # ------------------------------------------------------------------
    covered = {p["site_code"] for p in progress}
    sched_acc: dict = {}
    for t in leaf_tasks:        # 與「今日重點工項」共用同一次查詢的結果
        if t.site_code in covered:
            continue
        total = max((t.end_date - t.start_date).days + 1, 1)
        done = min(max((tt - t.start_date).days + 1, 0), total)
        acc = sched_acc.setdefault(t.site_code, [0, 0])
        acc[0] += done
        acc[1] += total
    for sc, (done, total) in sched_acc.items():
        if not total:
            continue
        s_site = site_by_code.get(sc)
        progress.append({
            "site": s_site.name if s_site else sc,
            "site_code": sc,
            "building": bld_labels.get(sc),
            "source": "schedule",
            "plan": round(done / total * 100, 1),
        })
    progress.sort(key=lambda x: x["site_code"])

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "range_days": days,
        "environment": environment,
        "environment_spec": thresholds_payload(),
        "headcount": headcount or None,
        "recent_forms": recent_forms,
        "today_tasks": today_tasks,
        "progress": progress,
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
            "no": f"F{f.id:06d}", "site": f.site.name, "building": f.building,
            "description": f.description,
            "vendor": f.vendor.name if f.vendor else "", "person": f.responsible_person,
            "due_date": f.due_date.isoformat() if f.due_date else None,
            "days_over": (today - f.due_date).days if f.due_date else 0,
        } for f in overdue], key=lambda x: -x["days_over"])[:20],
        "recent": [{
            "no": f"F{f.id:06d}", "site": f.site.name, "building": f.building,
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
    if not INGEST_TOKENS:
        raise HTTPException(status_code=503,
                            detail="未設定 INGEST_TOKENS，推送端點停用")
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


# 上傳檔（缺失照片、手寫簽名）需登入才能取用。原本用免驗證的靜態掛載，
# 內網任何人拿到網址就能看——雲端版有登入把關，地端必須比照。
# 網址形狀維持 /uploads/...，既有的資料庫路徑不必改。
@app.get("/uploads/{subpath:path}")
def serve_upload(subpath: str, user=Depends(need_login)):
    full = os.path.realpath(os.path.join(UPLOAD_DIR, subpath))
    # realpath 之後再驗證前綴，.. 與符號連結都繞不出上傳目錄
    if not full.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
        raise HTTPException(status_code=404, detail="檔案不存在")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="檔案不存在")
    return FileResponse(full)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "frontend")

# 只接受單純的檔名。這個值來自網址，放行斜線或點點就等於讓外部讀取
# frontend 以外的任何檔案。
_PAGE_NAME = re.compile(r"[a-z0-9_-]+")


def asset_version() -> str:
    """前端樣式與腳本的版本戳，取自這些檔案的最後修改時間。"""
    latest = 0
    try:
        for name in os.listdir(FRONTEND_DIR):
            if name.endswith((".css", ".js")):
                latest = max(latest,
                             int(os.path.getmtime(os.path.join(FRONTEND_DIR, name))))
    except OSError:
        return "0"
    return str(latest)


@app.get("/static/{page}.html")
def serve_page(page: str):
    """送出前端頁面，並在樣式與腳本的網址後面加上版本戳。

    為什麼不只靠 Cache-Control
    --------------------------
    no-cache 能讓「之後」的更新立刻生效，但救不了**已經**被快取起來的舊檔——
    瀏覽器是在存進去的當下就決定要留多久的。實際踩過：深色主題上線後，
    畫面拿到新的 HTML 卻配上舊的 CSS，警示帶底色還是淺的、文字卻已改成亮色，
    整段變成亮字配亮底完全看不見，而且重新整理也沒用。

    改成帶版本戳之後，檔案一改網址就跟著變，舊的快取條目根本對不上，
    不需要任何人去清快取——牆上那台機器不會有人去按重新整理。
    """
    if not _PAGE_NAME.fullmatch(page):
        raise HTTPException(status_code=404, detail="查無此頁")

    path = os.path.join(FRONTEND_DIR, f"{page}.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="查無此頁")

    with open(path, encoding="utf-8") as f:
        html = f.read()

    version = asset_version()
    html = re.sub(
        r'(href|src)="(/static/[^"?]+\.(?:css|js))"',
        lambda m: f'{m.group(1)}="{m.group(2)}?v={version}"',
        html,
    )
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


class RevalidatingStatic(StaticFiles):
    """靜態檔一律要求瀏覽器先向伺服器確認有沒有更新。

    為什麼需要這個
    --------------
    戰情室是一台開機就全螢幕、幾個月不會有人去碰的機器。瀏覽器預設會快取
    CSS 與 JS，更新樣式之後那台螢幕可能好幾天還在用舊檔——而且最糟的情況
    不是「看起來像舊版」，是**新舊混用**：新的 HTML 配舊的 CSS。實際發生過一次，
    深色主題上線後，畫面拿到新的 HTML 卻用舊的 CSS，警示帶底色仍是淺色、
    文字卻已改成亮色，整段變成亮字配亮底而看不見。

    no-cache 不是不快取，是「每次都先問」：檔案沒變時伺服器回 304，
    只有標頭的流量。這台機器在區域網路內，這個代價可以忽略。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", RevalidatingStatic(
    directory=os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "frontend"),
    html=True), name="static")


@app.get("/api/branding")
def branding():
    """品牌識別。前端據此渲染版頭，公司名稱不寫死在程式碼或版控中。"""
    return BRANDING


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
