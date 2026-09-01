"""進出場人次收集程式（人臉辨識裝置的彙總端點）。

為什麼有這一支
--------------
門禁資料庫（collectors/access.py）的 1433 埠被網段防火牆擋住，連續多次
測試都不通。而這台人臉辨識裝置（與監視器同一台）的 /admin/list 端點
**直接回傳每個門的彙總人次**（進場、出場、在場），而且我們連得到。
在門禁資料庫開通之前，這是可用的替代來源；開通之後兩者可並存互為備援。

**只取彙總計數，不碰個資**
--------------------------
這台裝置另有 /person/list/All 之類會回傳姓名與人臉紀錄的端點——那是
第三人的生物特徵個資，本程式一律不呼叫。/admin/list 每個門只回三個
數字（inCount / outCount / presenceCount）與門名，本程式**只寫入這三個
數字**，門名僅用於對應工地、不落地。

**這台裝置沒有驗證**
--------------------
/admin/list 不需要帳密即可存取（實測回 200）。這代表任何連得到這台
裝置的人都能讀到人次——是廠商 side 的設定問題，已向使用者標記，
應要求廠商加上存取控制或至少限制來源。我們這支程式只讀不寫，
不會擴大該風險，但不該把它當成安全的資料源看待。

設定（.env.onprem）
-------------------
    FACE_URL          裝置位址，例如 http://主機
    FACE_SITE_MAP     門名關鍵字 → 工地代碼的 JSON。門名（pname）由裝置回傳，
                      各門對應哪個工地只有現場知道，因此由設定提供，例：
                      {"2號門":"BD04","3號門":"BD05"}
                      同一工地的多個門會自動加總。
                      門名分不出棟別時，用 {"*":"BD04"} 把所有門一律加總到
                      該工地。
    FACE_INTERVAL     每輪間隔秒數，預設 300

用法（在 backend/onprem 目錄下執行）
------------------------------------
    python -m collectors.face            # 跑一輪就結束，用來驗證設定
    python -m collectors.face --loop     # 持續執行

未設定 FACE_SITE_MAP 時，本程式會印出裝置回傳的門名（那是地點名、非人名），
方便對照後填入設定；在對應好之前不寫入任何資料——寧可牆上顯示「尚無資料」，
也不要把某個門的人次掛到錯的工地。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from typing import Dict

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import DeviceReading, SessionLocal, Site, init_db

from .config import env, load_env, log

TIMEOUT = 20


def as_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fetch_gates() -> list:
    """取回各門的彙總人次。回傳 [{pname, inCount, outCount, presenceCount}, ...]。"""
    base = env("FACE_URL").rstrip("/")
    r = requests.get(f"{base}/admin/list", timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if body.get("code") not in (0, 200, None):
        raise RuntimeError(f"裝置回應非正常：code={body.get('code')} msg={body.get('msg')}")
    return body.get("data") or []


def poll_once() -> str:
    if not env("FACE_URL"):
        return "未設定 FACE_URL，已跳過"
    try:
        site_map = json.loads(env("FACE_SITE_MAP") or "{}")
    except json.JSONDecodeError:
        return "FACE_SITE_MAP 格式錯誤，未執行"

    gates = fetch_gates()
    if not gates:
        return "裝置未回傳任何門的資料"

    if not site_map:
        # 還沒對應：印出門名供對照（pname 是地點名，非個資），不寫入
        names = "、".join(str(g.get("pname", "?")) for g in gates)
        return (f"FACE_SITE_MAP 未設定，未寫入。裝置回傳 {len(gates)} 個門："
                f"{names}。請在設定填入「門名關鍵字→工地代碼」的對應。")

    # 依門名關鍵字歸到工地，同工地多門加總
    agg: Dict[str, Dict[str, int]] = {}
    unmapped = []
    catch_all = site_map.get("*")
    for g in gates:
        pname = str(g.get("pname", ""))
        # "*" 萬用鍵：所有門加總到同一工地（門名分不出棟別時用）
        code = catch_all or next(
            (c for kw, c in site_map.items() if kw != "*" and kw in pname), None)
        if code is None:
            unmapped.append(pname)
            continue
        slot = agg.setdefault(code, {"in": 0, "out": 0, "present": 0})
        slot["in"] += as_int(g.get("inCount"))
        slot["out"] += as_int(g.get("outCount"))
        slot["present"] += as_int(g.get("presenceCount"))

    now = datetime.now()
    db = SessionLocal()
    written = 0
    try:
        site_ids = {s.code: s.id for s in db.query(Site).all()}
        for code, c in agg.items():
            for metric, val in (("headcount_in", c["in"]),
                                ("headcount_out", c["out"]),
                                ("headcount_present", c["present"])):
                # 每個工地每個指標只保留「目前值」一列，就地更新而非每輪新增。
                # 儀表板只讀最新一筆、人數也沒有歷史趨勢圖；每 5 分鐘各插三列
                # 會讓 device_readings 無上限成長，數月後拖慢查詢。
                # 以 vendor_code 限定，不會動到門禁資料庫來源（access-db）的列。
                row = (db.query(DeviceReading)
                       .filter(DeviceReading.vendor_code == "face-device",
                               DeviceReading.site_code == code,
                               DeviceReading.metric == metric).first())
                if row:
                    row.value_num = val
                    row.reading_at = now
                    row.site_id = site_ids.get(code)
                else:
                    db.add(DeviceReading(
                        site_id=site_ids.get(code), site_code=code,
                        vendor_code="face-device", device_type="people",
                        device_id=code, metric=metric, value_num=val,
                        reading_at=now,
                        # 刻意不存門名與任何原始欄位：本系統只需要人數
                        raw_payload=None,
                    ))
                written += 1
        db.commit()
    finally:
        db.close()

    parts = [f"{code} 在場{c['present']}／進{c['in']}／出{c['out']}"
             for code, c in agg.items()]
    msg = f"寫入 {written} 筆；" + "、".join(parts)
    if unmapped:
        # 對應不到的門只記數量，不印門名——保守起見不擴大輸出
        msg += f"（另有 {len(unmapped)} 個門未對應，已略過）"
    return msg


def main() -> None:
    load_env()
    init_db()
    interval = int(env("FACE_INTERVAL", "300") or 300)
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
