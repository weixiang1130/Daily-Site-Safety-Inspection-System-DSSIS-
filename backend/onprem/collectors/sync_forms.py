"""把雲端填報系統的表單資料同步回地端。

為什麼是這個架構
----------------
填報留在雲端：工地是拿手機在現場填，要能從外網進得來。
戰情室搬到地端：大螢幕整天開著、每分鐘更新監視畫面與環境數據，
掛在雲端會持續吃掉方案額度，而這些資料的來源本來就都在公司網路內。

所以雲端只負責收表單，這支程式定時把資料抓回地端，儀表板一律讀地端資料庫。
好處是雲端額度只花在真正的填報上，而且雲端就算暫時連不上，
牆上仍看得到最後一次同步的內容。

比對方式
--------
工地與廠商用 code 比對（本來就是唯一鍵）。表單與缺失用 cloud_id 比對——
兩邊各自產生流水號，直接沿用 id 會撞號，把不同的資料蓋掉。

設定（見 .env.onprem.example）：

    CLOUD_API_URL      雲端站台網址
    CLOUD_SYNC_TOKEN   與站台 SITE_AGENT_TOKEN 相同的權杖
    SYNC_INTERVAL      每輪間隔秒數，預設 300

用法（在 backend/onprem 目錄下執行）：

    python -m collectors.sync_forms            # 跑一輪就結束
    python -m collectors.sync_forms --loop     # 持續執行
    python -m collectors.sync_forms --full     # 忽略同步進度，全部重抓
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import (BASE_DIR, Coordination, Finding, Inspection, SessionLocal,
                    Site, User, Vendor, init_db)

from .cloud_budget import spend
from .config import env, load_env, log

TIMEOUT = 60

# SYNC_INTERVAL 未設定時的預設輪距。工地填報不頻繁，12 小時是文件化的
# 建議節奏（.env.onprem.example）。site_runner 也 import 這個值——
# 兩個進入點各寫一個預設數字的話，改一邊另一邊不會跟上。
DEFAULT_INTERVAL = 43200

# 同步進度存成檔案而不是資料庫欄位：這是這支程式自己的狀態，
# 跟業務資料無關，混進資料表只會讓人以為它有什麼業務意義。
STATE_FILE = Path(BASE_DIR) / "sync_state.json"


def _state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 進度檔壞掉就當作沒同步過。全部重抓比較慢，但不會漏資料。
        log("同步進度檔讀取失敗，本輪改為全量同步")
        return {}


def read_state() -> Optional[str]:
    return _state().get("since")


def seconds_since_last_sync() -> Optional[float]:
    """距離上次成功同步過了幾秒；沒同步過回 None。

    給「重啟後要不要立刻同步」用。工地檢視器若因故障每十幾秒重啟一次，
    而每次啟動都無條件同步，就會變成對自家雲端額度的攻擊——實測一台
    這樣的機器每月產生約 20 萬次呼叫，是整站正常用量的十倍以上。
    """
    at = _state().get("last_sync_at")
    if not at:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(at)).total_seconds()
    except ValueError:
        return None


def write_state(since: Optional[str]) -> None:
    if since:
        STATE_FILE.write_text(
            json.dumps({"since": since,
                        "last_sync_at": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8")


SYNC_USER = "cloud-sync"


def sync_user_id(db) -> int:
    """取得（必要時建立）同步用的佔位帳號。

    inspections.inspector_id 不可為空且是外鍵，但地端不做帳號同步——牆上顯示的
    是表單裡填的 inspector_name。隨便填 0 在 SQLite 看起來沒事，到了正式環境的
    SQL Server 會因外鍵限制整批寫不進去。

    帳號本身無法登入：password_hash 填不可能對應到任何密碼的值。
    """
    u = db.query(User).filter(User.username == SYNC_USER).first()
    if not u:
        u = User(username=SYNC_USER, password_hash="!雲端同步專用，無法登入",
                 display_name="雲端同步", role="system", active=False)
        db.add(u)
        db.flush()
    return u.id


def parse_dt(v) -> Optional[datetime]:
    """雲端回傳的是 ISO 字串（含時區）。地端一律存本地時間。"""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def parse_date(v) -> Optional[date]:
    dt = parse_dt(v)
    return dt.date() if dt else None


def fetch(since: Optional[str]) -> dict:
    base = env("CLOUD_API_URL").rstrip("/")
    token = env("CLOUD_SYNC_TOKEN")
    if not base or not token:
        raise RuntimeError("未設定 CLOUD_API_URL 或 CLOUD_SYNC_TOKEN")

    # 每日硬上限：不論上游怎麼壞，這台機器每天最多只打固定次數。
    # 2026-09-03 就是「重啟即同步」的迴圈把免費額度燒光的。
    if not spend("sync_forms"):
        raise RuntimeError("今日雲端呼叫已達上限，本輪不同步（保護機制）")

    r = requests.get(
        f"{base}/api/v1/export",
        params={"since": since} if since else {},
        headers={"X-Agent-Token": token},
        timeout=TIMEOUT,
    )
    if r.status_code == 401:
        raise RuntimeError("權杖驗證失敗，請確認 CLOUD_SYNC_TOKEN 與站台的 "
                           "SITE_AGENT_TOKEN 相同")
    r.raise_for_status()
    return r.json()


def sync_once(full: bool = False) -> tuple:
    """同步一輪。回傳（訊息, 是否還有未取回的資料）。"""
    since = None if full else read_state()
    data = fetch(since)

    db = SessionLocal()
    counts = {"工地": 0, "廠商": 0, "檢查表": 0, "協議紀錄": 0, "缺失": 0}
    try:
        placeholder_id = sync_user_id(db)
        # --- 參照資料：以 code 比對 ---
        for row in data.get("sites", []):
            site = db.query(Site).filter(Site.code == row["code"]).first()
            if not site:
                site = Site(code=row["code"])
                db.add(site)
                counts["工地"] += 1
            site.name = row["name"]
            site.department = row.get("department")
            site.sort_order = row.get("sort_order") or 0
            site.active = bool(row.get("active", True))

        for row in data.get("vendors", []):
            v = db.query(Vendor).filter(Vendor.code == row["code"]).first()
            if not v:
                v = Vendor(code=row["code"])
                db.add(v)
                counts["廠商"] += 1
            v.name = row["name"]
            v.active = bool(row.get("active", True))
        db.flush()

        site_ids = {s.code: s.id for s in db.query(Site).all()}
        vendor_ids = {v.code: v.id for v in db.query(Vendor).all()}

        def site_id_of(row) -> Optional[int]:
            return site_ids.get(row.get("site_code"))

        # --- 檢查表 ---
        for row in data.get("inspections", []):
            sid = site_id_of(row)
            if sid is None:
                continue          # 工地還沒同步到，下一輪再處理
            obj = (db.query(Inspection)
                   .filter(Inspection.cloud_id == row["id"]).first())
            if not obj:
                obj = Inspection(cloud_id=row["id"], site_id=sid,
                                 form_code=row["form_code"],
                                 inspector_id=placeholder_id)
                db.add(obj)
                counts["檢查表"] += 1
            obj.site_id = sid
            obj.inspect_date = parse_date(row.get("inspect_date")) or date.today()
            obj.location = row.get("location")
            obj.inspector_name = row.get("inspector_name")
            obj.status = row.get("status") or "submitted"
            obj.submitted_at = parse_dt(row.get("submitted_at"))
            obj.created_at = parse_dt(row.get("created_at")) or datetime.now()
            obj.fail_count = row.get("fail_count")

        # --- 協議紀錄 ---
        for row in data.get("coordinations", []):
            sid = site_id_of(row)
            if sid is None:
                continue
            obj = (db.query(Coordination)
                   .filter(Coordination.cloud_id == row["id"]).first())
            if not obj:
                obj = Coordination(cloud_id=row["id"], site_id=sid)
                db.add(obj)
                counts["協議紀錄"] += 1
            obj.site_id = sid
            obj.meeting_date = parse_date(row.get("meeting_date")) or date.today()
            obj.work_date = parse_date(row.get("work_date")) or date.today()
            obj.recorder_name = row.get("recorder_name")
            obj.status = row.get("status") or "submitted"
            obj.submitted_at = parse_dt(row.get("submitted_at"))
            obj.created_at = parse_dt(row.get("created_at")) or datetime.now()
            obj.attendee_count = row.get("attendee_count")

        # --- 缺失（儀表板的主要資料來源）---
        for row in data.get("findings", []):
            sid = site_id_of(row)
            if sid is None:
                continue
            obj = db.query(Finding).filter(Finding.cloud_id == row["id"]).first()
            if not obj:
                obj = Finding(cloud_id=row["id"], site_id=sid, description="")
                db.add(obj)
                counts["缺失"] += 1
            obj.site_id = sid
            obj.source = row.get("source") or "inspection"
            obj.found_at = parse_dt(row.get("found_at")) or datetime.now()
            obj.location = row.get("location")
            obj.hazard_code = row.get("hazard_code")
            obj.hazard_label = row.get("hazard_label")
            obj.description = row.get("description") or ""
            obj.vendor_id = vendor_ids.get(row.get("vendor_code"))
            obj.severity = row.get("severity") or "minor"
            obj.action_type = row.get("action_type") or "onsite"
            obj.due_date = parse_date(row.get("due_date"))
            obj.fixed_at = parse_dt(row.get("fixed_at"))
            obj.verified_at = parse_dt(row.get("verified_at"))
            obj.status = row.get("status") or "open"
            obj.created_at = parse_dt(row.get("created_at")) or datetime.now()

        db.commit()
    finally:
        db.close()

    # 資料成功寫入才推進進度。先推進的話，寫入失敗那一批就永遠補不回來了。
    write_state(data.get("next_since"))

    msg = "、".join(f"{k} 新增 {v}" for k, v in counts.items() if v) or "無新增"
    total = sum(len(data.get(k, [])) for k in
                ("findings", "inspections", "coordinations"))
    msg = f"取得 {total} 筆異動，{msg}"

    # 這一批被上限截斷代表還有更多。但若進度沒有往前推，再抓一次也是同一批，
    # 會變成無窮迴圈——這種情況停下來，由下一輪重試。
    more = bool(data.get("truncated")) and data.get("next_since") != since
    if data.get("truncated"):
        msg += "（達單次上限，尚有未同步的資料）"
    return msg, more


def main() -> None:
    load_env()
    init_db()
    interval = int(env("SYNC_INTERVAL", str(DEFAULT_INTERVAL)) or DEFAULT_INTERVAL)
    loop = "--loop" in sys.argv
    full = "--full" in sys.argv

    while True:
        try:
            more = True
            while more:
                msg, more = sync_once(full=full)
                full = False      # 全量只做第一輪
                log(msg)
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
