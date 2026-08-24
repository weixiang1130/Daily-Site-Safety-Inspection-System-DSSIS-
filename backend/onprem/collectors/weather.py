"""微型氣象站收集程式（地端版）。

移植自 backend/cloud/lib/weather.ts。雲端那份會隨戰情室一起下線。

廠商 API 的三個地雷都保留在這裡，改寫時不要「順手整理掉」：
  1. 登入的 key 是 base64(帳號 + SOH + 密碼)，SOH 是 U+0001 而不是任何符號
  2. 平台不對 key 做 URL 解碼，base64 尾端的 "=" 一旦被編成 %3D 就回 ErrUser，
     因此送出的表單必須手動組字串
  3. 回應的 JSON 不一定合法（series 之間偶爾缺逗號），只能逐段擷取

設定（放在 .env.onprem，見 config.py）：

    WEATHER_API_URL       平台位址，例如 http://主機:埠
    WEATHER_API_USER      帳號
    WEATHER_API_PASS      密碼
    WEATHER_SITE_MAP      測站 mac 對應工地代碼的 JSON，例如 {"AAAA":"BD04"}
    WEATHER_INTERVAL      每輪間隔秒數，預設 900（15 分鐘）

用法（在 backend/onprem 目錄下執行）：

    python -m collectors.weather            # 跑一輪就結束，用來驗證設定
    python -m collectors.weather --loop     # 持續執行，交給工作排程器開機啟動
"""

from __future__ import annotations

import base64
import json
import random
import re
import sys
import time
from datetime import datetime
from typing import Dict, List
from urllib.parse import unquote, urlencode

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import DeviceReading, SessionLocal, Site, init_db
from app.hazard import heat_index_c, station_level

from .config import env, load_env, log

# channel 參數的分隔字元是 SOH（U+0001），不是任何可見符號
SEP = chr(1)

# 廠商頻道名稱 → 本系統 metric 代碼。未列出者略過不存。
METRIC_MAP: Dict[str, str] = {
    "PM2.5": "pm25",
    "PM10": "pm10",
    "噪音": "noise",
    "溫度": "temperature",
    "濕度": "humidity",
    "熱指數": "heat_index",
    # 廠商的危害等級實測不可信（熱指數 49.4 仍回報 0），只留作與廠商釐清時的
    # 佐證，實際分級由 app/hazard.py 自行計算，見 derive_metrics()
    "危害等級": "vendor_hazard_level",
    "噪音時段日間": "noise_alarm_day",
    "噪音時段晚間": "noise_alarm_evening",
    "噪音時段夜間": "noise_alarm_night",
    "日間警報": "noise_alarm_day",
    "晚間警報": "noise_alarm_evening",
    "夜間警報": "noise_alarm_night",
}

TIMEOUT = 30

RE_USER_IDX = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
RE_STATION = re.compile(
    r'"DeviceName":"(.*?)".*?"mac":"(.*?)","ConnectState":"(.*?)".*?'
    r'"ReadAILastTime":"(.*?)"'
)
RE_SERIES = re.compile(
    r'\{"Name":"(.*?)","Label".*?"Data":\[\{"Time":\[(.*?)\],"Value":\[(.*?)\]\}\]\}'
)
RE_TAIPEI_TIME = re.compile(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def post(base: str, cmd: str, body: str) -> str:
    r = requests.post(
        f"{base}/{cmd}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.text


def login(base: str) -> str:
    """登入取得 session（UserIdx）。

    帳密不是分開的欄位，而是合併後 base64：key = base64(帳號 + SOH + 密碼)，
    與 channel 參數用的是同一個 SOH 分隔字元。
    """
    raw = f"{env('WEATHER_API_USER')}{SEP}{env('WEATHER_API_PASS')}"
    key = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    # 手動組字串：用 urlencode 會把尾端的 "=" 編成 %3D，平台不解碼，直接回 ErrUser
    text = post(base, "Login", f"key={key}&val={random.random()}")

    m = RE_USER_IDX.search(text)
    if not m:
        raise RuntimeError(f"登入失敗，未取得 UserIdx（回應：{text[:120]}）")
    return m.group(0)


def safe_decode(s: str) -> str:
    try:
        return unquote(s)
    except Exception:                                   # noqa: BLE001
        return s


def online_stations(base: str, uid: str) -> List[dict]:
    """取得目前「已連線」的測站。斷線者直接略過，不佔用後續請求。"""
    text = post(base, "ReadStateALL", f"UserIdx={uid}&val={random.random()}")
    out = []
    for name, mac, state, last_at in RE_STATION.findall(text):
        if state != "已連線":
            continue
        out.append({"mac": mac, "name": safe_decode(name), "last_at": last_at})
    return out


def taipei_to_dt(s: str) -> datetime:
    """"2026/08/19 10:00:00"（台北時間）→ datetime。

    地端資料庫的時間欄位一律存本地時間，且這台機器就在台灣，
    因此直接解析、不做時區轉換。
    """
    m = RE_TAIPEI_TIME.search(s)
    if not m:
        return datetime.now()
    return datetime(*(int(x) for x in m.groups()))


def read_station(base: str, uid: str, mac: str) -> List[dict]:
    """取得單一測站當日的最新量測值。

    不用 json.loads：廠商回應的 series 之間偶爾缺逗號（部分測站必現），
    整份解析會直接失敗。頻道意義以回應中的 Name 判斷——各測站頻道配置不同，
    不可假設索引相同。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    channels = ",".join(f"{mac}{SEP}{i}" for i in range(8))

    body = urlencode({
        "startT": today, "endT": today, "channel": channels,
        "SampleOptions": "{}", "Type": "1",
        "isWriteUserHis": "0",          # 不寫入對方平台的使用者查詢紀錄
        "dataSpanSec": "300",
        "UserIdx": uid, "val": str(random.random()),
    })

    text = post(base, "TrendData", body)
    out: List[dict] = []
    seen = set()

    for name, times_raw, vals_raw in RE_SERIES.findall(text):
        ch_name = "-".join(safe_decode(name).split("-")[1:])
        metric = METRIC_MAP.get(ch_name)
        if not metric or metric in seen:
            continue

        times = [t.strip('"') for t in times_raw.split(",")]
        vals = [v.strip('"') for v in vals_raw.split(",")]

        # 尾端是補齊到區間結尾的空值，往前找最後一個有值的
        i = len(vals) - 1
        while i >= 0 and vals[i] == "":
            i -= 1
        if i < 0 or i >= len(times):
            continue
        try:
            v = float(vals[i])
        except ValueError:
            continue

        seen.add(metric)
        out.append({"metric": metric, "value": v, "at": taipei_to_dt(times[i])})
    return out


def derive_metrics(readings: List[dict]) -> List[dict]:
    """由原始讀值推導本系統自己的指標。

    兩件事：
      1. 廠商若未提供熱指數，只要有溫濕度就自行推算，不受制於對方
      2. 依 app/hazard.py 的門檻算出 hazard_level，取代廠商不可信的值

    衍生值沿用來源讀值的時間戳，才能與原始指標對齊在同一個時間點上。
    """
    if not readings:
        return []
    by = {r["metric"]: r["value"] for r in readings}
    at = readings[0]["at"]
    out = []

    if by.get("heat_index") is None and by.get("temperature") is not None \
            and by.get("humidity") is not None:
        hi = heat_index_c(by["temperature"], by["humidity"])
        if hi is not None:
            by["heat_index"] = hi
            out.append({"metric": "heat_index", "value": hi, "at": at})

    # vendor_hazard_level 不列入計算，否則等於把廠商的錯誤值又引回來
    judged = {k: v for k, v in by.items() if k != "vendor_hazard_level"}
    out.append({"metric": "hazard_level", "value": station_level(judged), "at": at})
    return out


def poll_once() -> str:
    base = env("WEATHER_API_URL").rstrip("/")
    if not base:
        return "未設定 WEATHER_API_URL，已跳過"

    try:
        site_map = json.loads(env("WEATHER_SITE_MAP") or "{}")
    except json.JSONDecodeError:
        log("WEATHER_SITE_MAP 格式錯誤，測站將無法對應工地")
        site_map = {}

    uid = login(base)
    stations = online_stations(base, uid)
    log(f"在線測站 {len(stations)} 台")

    db = SessionLocal()
    inserted = skipped = 0
    try:
        site_ids = {s.code: s.id for s in db.query(Site).all()}
        for st in stations:
            site_code = site_map.get(st["mac"])
            site_id = site_ids.get(site_code) if site_code else None

            try:
                readings = read_station(base, uid, st["mac"])
                readings += derive_metrics(readings)
            except Exception as e:                      # noqa: BLE001
                # 單一測站失敗不影響其他測站——工地不會有人盯著這支程式，
                # 它必須自己撐下去
                log(f"{st['name']} 讀取失敗：{e}")
                continue

            for r in readings:
                # 只寫入比既有紀錄更新的資料，避免每輪都塞重複值把表撐大
                last = (db.query(DeviceReading.reading_at)
                        .filter(DeviceReading.device_id == st["mac"],
                                DeviceReading.metric == r["metric"])
                        .order_by(DeviceReading.reading_at.desc()).first())
                if last and last[0] and last[0] >= r["at"]:
                    skipped += 1
                    continue

                db.add(DeviceReading(
                    site_id=site_id, site_code=site_code,
                    vendor_code="weather-station", device_type="env",
                    device_id=st["mac"], metric=r["metric"], value_num=r["value"],
                    reading_at=r["at"],
                    raw_payload=json.dumps({"station": st["name"]},
                                           ensure_ascii=False),
                ))
                inserted += 1
        db.commit()
    finally:
        db.close()

    return (f"在線測站 {len(stations)} 台，寫入 {inserted} 筆、"
            f"略過 {skipped} 筆（已是最新）")


def main() -> None:
    load_env()
    init_db()
    interval = int(env("WEATHER_INTERVAL", "900") or 900)
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
