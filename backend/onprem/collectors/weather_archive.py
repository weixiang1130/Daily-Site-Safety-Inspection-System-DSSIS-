"""微型氣象站歷史歸檔（分析用）：每天一次，把完整日的 5 分鐘資料存進 weather_readings。

為什麼另寫一支
--------------
collectors/weather.py 是給戰情室牆面的：每 15 分鐘抓「今天最新一筆」，
電腦關機那段就沒有，也不會補。拿來分析天氣與出工、工種的關係不夠——
週末關機整天空白，而且只有平台原始解析度（5 分鐘）的三分之一。

這支只抓**已經過完的日子**（到昨天為止），一次拿整天的 5 分鐘序列。
平台保留一年以上的歷史，所以電腦關機幾天，下次執行會自動補齊；第一次
執行從 WEATHER_ARCHIVE_FROM 開始回補。

每次都重抓最近 OVERLAP_DAYS 天；測站斷線過的話，從最後有資料那天起重抓
（最多追 LATE_UPLOAD_DAYS 天）——恢復連線後才補傳到平台的資料也會進來。
重抓以日為單位「刪掉重寫」，不會重複；沒拿到資料的日子不刪既有資料。

故障值的過濾（VALID_RANGE、clean_values）與熱指數推算（derive_metrics）
沿用 weather.py，牆面與歷史用同一套規則。

設定（.env.onprem，測站與帳密沿用 weather.py 的設定）：

    WEATHER_ARCHIVE_FROM   第一次回補的起始日，預設 2024-01-01。
                           平台沒有資料的日子查詢很快，起得早沒有代價。

用法（在 backend/onprem 目錄下執行）：

    python -m collectors.weather_archive
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Dict, List
from urllib.parse import urlencode

from sqlalchemy import func, text

from app.db import (IS_MSSQL, SessionLocal, WeatherArchiveProgress,
                    WeatherReading, engine, init_db)
from app.hazard import THRESHOLDS

from . import weather as w
from .config import env, load_env, log

CHUNK_DAYS = 7          # 一次查 7 天約 2,000 個時間點，回應約數百 KB
OVERLAP_DAYS = 3
LATE_UPLOAD_DAYS = 14   # 斷線測站最多往回追幾天的補傳資料
PAUSE_SEC = 0.5         # 回補上百次查詢時，別對廠商平台連續猛打

ARCHIVE_COLUMNS = ("pm25", "pm10", "noise", "temperature", "humidity", "heat_index")
# 廠商頻道名稱 → 欄位，由 weather.METRIC_MAP 導出：牆面為新的頻道寫法加別名時，
# 歷史歸檔一併生效。噪音時段警報實測恆為 0，不存。
COLUMN_OF: Dict[str, str] = {name: metric for name, metric in w.METRIC_MAP.items()
                             if metric in ARCHIVE_COLUMNS}

# 分析檢視表的工作時段（07:00–17:00）
WORK_HOURS = "DATEPART(hour, reading_at) BETWEEN 7 AND 16"


def fetch_range(base: str, uid: str, mac: str, d1: date, d2: date) -> Dict[datetime, dict]:
    """查 d1~d2（含）的 5 分鐘序列，回傳 {時間: {欄位: 值}}（尚未過濾故障值）。

    平台的 endT 會多給隔天 00:00 那一格；那格屬於下一段，這裡濾掉，
    由下一次查詢負責，避免同一格被兩段各寫一次。

    回應裡一個頻道都沒有時丟例外：平台出錯是回 HTTP 200 加錯誤文字（登入
    失敗就是這樣），當成「查無資料」的話，會把該區間記成已歸檔而永遠不再查。
    斷線測站的正常回應仍會列出頻道，只是值是空的。
    """
    body = urlencode({
        "startT": d1.isoformat(), "endT": d2.isoformat(),
        "channel": ",".join(f"{mac}{w.SEP}{i}" for i in range(8)),
        "SampleOptions": "{}", "Type": "1",
        "isWriteUserHis": "0",          # 不寫入對方平台的使用者查詢紀錄
        "dataSpanSec": "300",
        "UserIdx": uid, "val": str(random.random()),
    })
    payload = w.post(base, "TrendData", body)
    series = w.RE_SERIES.findall(payload)
    if not series:
        raise RuntimeError(f"TrendData 回應沒有任何頻道（{d1}～{d2}），"
                           f"可能是登入逾時或平台異常：{payload[:80]!r}")
    start = datetime.combine(d1, datetime.min.time())
    end = datetime.combine(d2 + timedelta(days=1), datetime.min.time())

    rows: Dict[datetime, dict] = {}
    seen = set()
    for name, times_raw, vals_raw in series:
        col = COLUMN_OF.get("-".join(w.safe_decode(name).split("-")[1:]))
        if not col or col in seen:
            continue
        seen.add(col)
        times = [t.strip('"') for t in times_raw.split(",")]
        vals = [v.strip('"') for v in vals_raw.split(",")]
        for t, v in zip(times, vals):
            if v == "" or not w.RE_TAIPEI_TIME.search(t):
                continue
            at = w.taipei_to_dt(t)
            if not (start <= at < end):
                continue
            try:
                rows.setdefault(at, {})[col] = float(v)
            except ValueError:
                continue
    return rows


def to_row(site_code, mac: str, at: datetime, vals: dict) -> dict:
    """已過濾的讀值 → 資料列。熱指數缺值時推算、危害等級自行判定，與牆面同一套。"""
    vals = dict(vals)
    level = 0
    for d in w.derive_metrics([{"metric": c, "value": v, "at": at} for c, v in vals.items()]):
        if d["metric"] == "hazard_level":
            level = int(d["value"])
        else:
            vals[d["metric"]] = d["value"]
    return dict(site_code=site_code, device_id=mac, reading_at=at,
                hazard_level=level, fetched_at=datetime.now(),
                **{c: vals.get(c) for c in ARCHIVE_COLUMNS})


def archive_station(base: str, uid: str, mac: str, site_code, first: date,
                    yesterday: date) -> int:
    db = SessionLocal()
    try:
        prog = db.get(WeatherArchiveProgress, mac)
        last_at = (db.query(func.max(WeatherReading.reading_at))
                   .filter(WeatherReading.device_id == mac).scalar())
    finally:
        db.close()

    if prog is None:
        start = first
    else:
        start = prog.archived_through - timedelta(days=OVERLAP_DAYS - 1)
        # 斷線的測站恢復連線後會補傳緩存資料：從最後有資料那天起重查。
        # 只追 LATE_UPLOAD_DAYS 天，長期停用的測站不必每天從停用日查起。
        if last_at is not None and (yesterday - last_at.date()).days <= LATE_UPLOAD_DAYS:
            start = min(start, last_at.date())
        start = max(first, start)

    written = 0
    d1 = start
    while d1 <= yesterday:
        d2 = min(d1 + timedelta(days=CHUNK_DAYS - 1), yesterday)
        rows = fetch_range(base, uid, mac, d1, d2)
        batch = [to_row(site_code, mac, at, v) for at, v in
                 ((at, w.clean_values(vals)) for at, vals in sorted(rows.items())) if v]
        db = SessionLocal()
        try:
            # 以「日」為單位替換：這次有拿到資料的日子整天刪掉重寫（可能多了補傳
            # 的點，也可能少了被平台修正掉的點）；沒拿到資料的日子不動——平台不會
            # 真的收回資料，查無多半是測站斷線或暫時異常，刪了就是把好資料換成空白。
            for day in sorted({r["reading_at"].date() for r in batch}):
                db.query(WeatherReading).filter(
                    WeatherReading.device_id == mac,
                    WeatherReading.reading_at >= datetime.combine(day, datetime.min.time()),
                    WeatherReading.reading_at < datetime.combine(day + timedelta(days=1),
                                                                 datetime.min.time()),
                ).delete(synchronize_session=False)
            if batch:
                db.bulk_insert_mappings(WeatherReading, batch)
            prog = db.get(WeatherArchiveProgress, mac)
            if prog is None:
                prog = WeatherArchiveProgress(device_id=mac)
                db.add(prog)
            prog.site_code = site_code
            # 只前進不後退：斷線重查的區間在進度之前，不能把進度拉回去
            prog.archived_through = max(d2, prog.archived_through or d2)
            prog.updated_at = datetime.now()
            # 資料與進度同一個交易：寫一半失敗就兩者都不動，下次從同一天重來
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        written += len(batch)
        d1 = d2 + timedelta(days=1)
        time.sleep(PAUSE_SEC)
    return written


def _safe_code(v: str) -> str:
    # 檢視表定義裡直接嵌入工地代碼，只允許代碼字元，避免設定檔內容變成 SQL
    return v if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", v or "") else ""


def ensure_views() -> None:
    """建立（或更新）分析用檢視表。只支援 SQL Server；SQLite 的工地檢視器不歸檔。

    工作時段取 07:00–17:00（WORK_HOURS）。hours_* 以「符合的 5 分鐘格數 × 5 分」
    換算，測站斷線的時段不計入，所以 samples 偏低的日子時數也會偏低。
    熱指數第二、三級分界直接取 app/hazard.py 的 THRESHOLDS——指引修正時只改那裡，
    這裡每天重建檢視表會自動跟上。
    噪音平均是能量平均（等效音壓級）：分貝是對數尺度，直接平均會低估，
    半天 90 dB、半天 60 dB 算術平均是 75，等效音壓級約 87。
    """
    if not IS_MSSQL:
        return
    breaks = THRESHOLDS["heat_index"].breaks
    lv2, lv3 = float(breaks[1]), float(breaks[2])
    primary = _safe_code(env("PRIMARY_SITE_CODE"))
    daily = f"""
CREATE OR ALTER VIEW v_weather_daily AS
SELECT site_code, device_id,
       CAST(reading_at AS date) AS obs_date,
       COUNT(*) AS samples,
       MIN(temperature) AS temp_min, AVG(temperature) AS temp_avg, MAX(temperature) AS temp_max,
       AVG(humidity) AS humidity_avg,
       MAX(heat_index) AS heat_index_max,
       MAX(CASE WHEN {WORK_HOURS} THEN heat_index END) AS heat_index_max_work,
       AVG(CASE WHEN {WORK_HOURS} THEN heat_index END) AS heat_index_avg_work,
       SUM(CASE WHEN {WORK_HOURS} AND heat_index >= {lv2!r} THEN 5 ELSE 0 END) / 60.0 AS hours_hi_lv2_work,
       SUM(CASE WHEN {WORK_HOURS} AND heat_index >= {lv3!r} THEN 5 ELSE 0 END) / 60.0 AS hours_hi_lv3_work,
       AVG(pm25) AS pm25_avg, MAX(pm25) AS pm25_max,
       AVG(pm10) AS pm10_avg, MAX(pm10) AS pm10_max,
       10 * LOG10(AVG(CASE WHEN {WORK_HOURS}
                           THEN POWER(CAST(10 AS float), CAST(noise AS float) / 10) END)) AS noise_leq_work,
       MAX(noise) AS noise_max,
       MAX(hazard_level) AS hazard_level_max
FROM weather_readings
GROUP BY site_code, device_id, CAST(reading_at AS date)
"""
    # 出工對照：以主場站（PRIMARY_SITE_CODE）的天氣為準，每天一列。
    # 以天氣的日期為底——沒有任何出工回報的日子（例如豪雨停工）也要留著，
    # 那正是分析要看的；只取出工資料開始之後的日子。
    joined = f"""
CREATE OR ALTER VIEW v_worklog_weather_daily AS
WITH wl AS (
    SELECT report_date, COUNT(*) AS reports, SUM(headcount) AS headcount
    FROM worklog_reports GROUP BY report_date
)
SELECT wd.obs_date AS report_date,
       -- 1=週一 … 7=週日。不用 DATEPART(weekday)：它隨 SET DATEFIRST／語系改變
       DATEDIFF(day, '19000101', wd.obs_date) % 7 + 1 AS weekday_mon1,
       COALESCE(wl.reports, 0) AS reports,
       COALESCE(wl.headcount, 0) AS headcount,
       wd.samples, wd.temp_max, wd.temp_avg, wd.humidity_avg,
       wd.heat_index_max_work, wd.hours_hi_lv2_work, wd.hours_hi_lv3_work,
       wd.pm25_avg, wd.pm10_avg, wd.noise_leq_work, wd.hazard_level_max
FROM v_weather_daily wd
LEFT JOIN wl ON wl.report_date = wd.obs_date
WHERE wd.site_code = '{primary}'
  AND wd.obs_date >= (SELECT MIN(report_date) FROM worklog_reports)
"""
    with engine.begin() as conn:
        conn.execute(text(daily))
        if primary:
            conn.execute(text(joined))


def run_once() -> str:
    base = env("WEATHER_API_URL").rstrip("/")
    if not base:
        return "未設定 WEATHER_API_URL，已跳過"
    try:
        site_map = json.loads(env("WEATHER_SITE_MAP") or "{}")
    except json.JSONDecodeError:
        return "WEATHER_SITE_MAP 格式錯誤，已跳過"
    if not site_map:
        return "WEATHER_SITE_MAP 未設定測站，已跳過"
    try:
        first = date.fromisoformat(env("WEATHER_ARCHIVE_FROM") or "2024-01-01")
    except ValueError:
        return "WEATHER_ARCHIVE_FROM 格式應為 YYYY-MM-DD，已跳過"

    yesterday = date.today() - timedelta(days=1)
    uid = w.login(base)
    parts: List[str] = []
    failed = 0
    for mac, site_code in site_map.items():
        try:
            n = archive_station(base, uid, mac, site_code, first, yesterday)
            parts.append(f"{site_code} {n} 筆")
        except Exception as e:                          # noqa: BLE001
            # 單一測站失敗不影響其他測站；它的進度沒推進，下次從同一天重來
            failed += 1
            log(f"{site_code} 歸檔失敗：{e}")
    ensure_views()
    msg = f"氣象歸檔至 {yesterday}：" + "、".join(parts)
    return msg + (f"；{failed} 台失敗" if failed else "")


def main() -> None:
    load_env()
    init_db()
    try:
        log(run_once())
    except Exception as e:                              # noqa: BLE001
        log(f"氣象歸檔失敗：{e}")


if __name__ == "__main__":
    main()
