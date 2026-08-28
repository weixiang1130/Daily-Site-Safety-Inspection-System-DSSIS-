"""工程進度收集程式（公司 FinOps 資料庫，唯讀）。

抓什麼
------
`BI.dbo.vw_ProjSurveyResult` 的**財務進度**：累計實際成本佔比與累計預估
成本佔比，一案一月一列。這是全庫唯一有進度率的表。

**這是以金額衡量的進度，不是施工進度**（資料庫裡沒有工項／樓層完成率）。
牆上顯示時必須標明「金額」，否則會被當成現場做到幾成——那是兩件事。

兩個查詢陷阱（來源：kindom-finops-query skill 的實測紀錄）
----------------------------------------------------------
1. 此 view 含全期逐月「預測」，直接取 MAX(MonthEnd) 會拿到專案結束後的
   未來月份。必須限定 MonthEnd 不晚於今天再取最新一列。
2. 判讀要比「實際 vs 預估」，不是「實際 vs 時間進度」。成本投入是 S 曲線，
   時間過半不代表成本該過半；實測有時間 44%、實際 21% 看似大幅落後，
   但預估曲線在該點是 15~18%，實際是超前的。

設定（.env.onprem）
-------------------
    FINOPS_SERVER       例：主機\\執行個體,1433
    FINOPS_USER         唯讀帳號
    FINOPS_PWD          密碼——由使用者自行填入，本程式與工具不代填
    FINOPS_PROJECT_MAP  專案代碼對應工地代碼的 JSON，例：{"131H":"BD04"}
    FINOPS_INTERVAL     每輪間隔秒數，預設 43200（12 小時；月結資料，再頻繁也不會變）

用法（在 backend/onprem 目錄下執行）
------------------------------------
    python -m collectors.finops            # 跑一輪就結束，用來驗證設定
    python -m collectors.finops --loop     # 持續執行
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime

from app.db import DeviceReading, SessionLocal, Site, init_db

from .config import env, load_env, log

# 進度率存進 device_readings 的 metric 名稱。
# actual＝累計實際成本佔比、est＝累計預估成本佔比、time＝時間進度。
METRICS = ("progress_actual", "progress_est", "progress_time")


def connect():
    try:
        import pyodbc
    except ImportError:
        sys.exit("需要 pyodbc，請先執行：pip install pyodbc")

    server = env("FINOPS_SERVER")
    pwd = env("FINOPS_PWD")
    if not server:
        return None
    if not pwd:
        sys.exit("FINOPS_PWD 未設定。密碼請自行填入 .env.onprem——"
                 "依內部規範，工具不代填、不記錄這組密碼。")

    driver = [d for d in pyodbc.drivers() if "for SQL Server" in d][-1]
    cs = (f"DRIVER={{{driver}}};SERVER={server};DATABASE=BI;"
          f"UID={env('FINOPS_USER')};PWD={pwd};"
          "Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=15")
    return pyodbc.connect(cs, timeout=15)


def fetch_progress(conn, project_id: str):
    """取某專案「不晚於今天」的最新一筆月結進度。

    view 含全期逐月預測，不濾日期會拿到未來月份的預測值當成現況。
    ProjectID 欄位常有尾隨空白，比對一律去空白。
    """
    cur = conn.cursor()
    # 取未捨入的 decimal 欄位（*Rate），不取顯示用的 *RateShow——Show 欄捨入到
    # 整數，超前／落後以兩者相減判定，在 ±3% 門檻附近會因捨入誤判紅／琥珀
    # （實測 131H：Show 21/15 → 差 6.0；decimal 0.206/0.149 → 差 5.7）。
    cur.execute("""
        SELECT TOP 1 MonthEnd, TimeRate, AccRealCostRate, AccEstCostRate
        FROM BI.dbo.vw_ProjSurveyResult
        WHERE LTRIM(RTRIM(ProjectID)) = ?
          AND MonthEnd <= CONVERT(char(10), GETDATE(), 111)
        ORDER BY MonthEnd DESC""", project_id.strip())
    row = cur.fetchone()
    if not row:
        return None
    month_end, t_rate, actual, est = row

    def num(v):
        """decimal 比率（0.206）→ 百分比（20.6）。"""
        try:
            return round(float(v) * 100, 1)
        except (TypeError, ValueError):
            return None

    # MonthEnd 是 '2026/07/31' 這類字串
    m = str(month_end).replace("-", "/").split(" ")[0]
    parts = m.split("/")
    when = date(int(parts[0]), int(parts[1]), int(parts[2]))
    return {"month_end": when, "time": num(t_rate),
            "actual": num(actual), "est": num(est)}


def poll_once() -> str:
    if not env("FINOPS_SERVER"):
        return "未設定 FINOPS_SERVER，已跳過"
    try:
        pmap = json.loads(env("FINOPS_PROJECT_MAP") or "{}")
    except json.JSONDecodeError:
        return "FINOPS_PROJECT_MAP 格式錯誤，未執行"
    if not pmap:
        return "FINOPS_PROJECT_MAP 未設定任何專案，已跳過"

    conn = connect()
    results = []
    try:
        for pid, site_code in pmap.items():
            row = fetch_progress(conn, pid)
            if row is None:
                log(f"{pid} 查無月結資料")
                continue
            results.append((pid, site_code, row))
    finally:
        conn.close()

    db = SessionLocal()
    written = skipped = 0
    try:
        site_ids = {s.code: s.id for s in db.query(Site).all()}
        for pid, site_code, row in results:
            at = datetime.combine(row["month_end"], datetime.min.time())
            for metric, val in (("progress_actual", row["actual"]),
                                ("progress_est", row["est"]),
                                ("progress_time", row["time"])):
                if val is None:
                    continue
                # 月結同一個月只留一筆；已存在就更新——上游月結會重編，
                # 只跳過的話，第一次抓到的值會被永遠鎖死
                dup = (db.query(DeviceReading)
                       .filter(DeviceReading.device_id == pid,
                               DeviceReading.metric == metric,
                               DeviceReading.reading_at == at).first())
                if dup:
                    if float(dup.value_num or 0) != val:
                        dup.value_num = val
                        written += 1
                    else:
                        skipped += 1
                    continue
                db.add(DeviceReading(
                    site_id=site_ids.get(site_code), site_code=site_code,
                    vendor_code="finops", device_type="progress",
                    device_id=pid, metric=metric, value_num=val,
                    reading_at=at, raw_payload=None,
                ))
                written += 1
        db.commit()
    finally:
        db.close()

    parts = [f"{sc}：實際 {r['actual']}%、預估 {r['est']}%"
             f"（截至 {r['month_end'].year - 1911}/{r['month_end'].month:02d} 月結）"
             for _, sc, r in results]
    return f"寫入 {written} 筆、略過 {skipped} 筆（已有）；" + "；".join(parts)


def main() -> None:
    load_env()
    init_db()
    interval = int(env("FINOPS_INTERVAL", "43200") or 43200)
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
