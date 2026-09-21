# -*- coding: utf-8 -*-
"""雲端排程：只產生看板的「外部來源」三區塊，推上雲端 Blob。

為什麼有這支
------------
工地看板原本靠「地端主機」把整份快照推上雲端；地端跑在誰的電腦上，
那台電腦一關，雲端看板就顯示「資料已停止更新」。這支把**不需要公司
內網、也不需要雲端資料庫**的三塊搬到 GitHub Actions 每 30 分鐘跑一次：

    station   微型氣象站（公開平台）
    worklog   本日出工（Google 試算表）
    news      職安署新知（公開網站）

缺失統計、無災害、看板設定這些「要資料庫」的區塊不在這裡——它們由雲端
自己以事件驅動維護（填報時順手更新），看板讀取時再與本檔推上去的外部
區塊合併。因此這支**完全不碰雲端資料庫**，不增加 Netlify DB compute。

沿用地端既有、實測過的收集程式（weather.py / worklog.py / osha_news.py），
一行解析邏輯都不重寫——只是換一台永遠開機、不是你電腦的機器來跑。

資料流
------
1. 用一個「用完即丟」的 SQLite（DB_BACKEND=sqlite，跑在暫存目錄）當收集
   程式的落地處——不連任何正式資料庫。
2. 依序 poll 三支收集程式，資料寫進這個暫存 SQLite。
3. 讀回 station / worklog / news 三段，組成 external 區塊。
4. 有設 CLOUD_INGEST_URL＋CLOUD_INGEST_TOKEN 就 POST 上雲端；否則印到
   stdout（本機驗證用）。

需要的環境變數（GitHub Actions Secrets）
--------------------------------------
    WEATHER_API_URL / WEATHER_API_USER / WEATHER_API_PASS / WEATHER_SITE_MAP
    WORKLOG_SHEET_URL            出工試算表（含人名/LINE ID，務必設 secret）
    BUILDING_LABELS              例：BD04:辦公棟,BD05:住宅棟
    PRIMARY_SITE_CODE            主場站代碼，例：BD04（挑要上牆的測站）
    CLOUD_INGEST_URL             例：https://<站台>/api/v1/ingest/external
    CLOUD_INGEST_TOKEN           對應雲端 WALL_INGEST_TOKEN

失敗處理
--------
任何一支收集程式失敗時，**不推那一塊**（雲端保留上一份好的資料與它的
時間戳，看板會因該塊過期而亮警示），其餘區塊照推。

失敗分兩種（_bootstrap.is_transient）：
- **暫時性**（連不上、逾時、對方 5xx）：30 秒後重試一次；仍失敗只留黃色
  註記、以 0 結束——不寄信。實測 2026-09-18 晚上連續 1.5 小時、9/20 凌晨
  各有一段抓不到，都自己恢復；每次都寄「All jobs have failed」只是噪音。
  持續抓不到時由看板的過期警示（75 分）反映。
- **需要處理**（權杖錯、網址失效、試算表進垃圾桶、缺設定、程式錯）：紅色
  註記、以非 0 結束，Actions 轉紅寄信。

註記會寫出是哪一塊、什麼原因，執行摘要與通知信不必登入就看得到。錯誤訊息
一律隱去網址——公開 repo 的 Actions 日誌任何人可讀，而出工試算表網址等同
資料存取權。

用法
----
    python backend/cloud-runner/run_external.py            # 跑一次、印出（本機驗證）
    python backend/cloud-runner/run_external.py --push     # 跑一次並推上雲端
"""
from __future__ import annotations

import _bootstrap as bs  # noqa: I001 —— 必須最先：時區、暫存 SQLite、import 路徑

import json
import os
import sys
import time
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import SessionLocal, Site, WorkLog, init_db  # noqa: E402
from app.envfile import load_env  # noqa: E402

TIMEOUT = 30
HISTORY_DAYS = 70          # 每日出工總數回推天數（雲端累積，供上月人日）


def _seed_primary_site(db) -> None:
    """氣象站收集把測站對到工地代碼；暫存庫是空的，先塞一筆主場站，
    讓 environment_snapshot 能把 site_code 對回工地（對不到只是少了
    工地名，不影響數值）。"""
    code = (os.environ.get("PRIMARY_SITE_CODE") or "").strip()
    if not code or db.query(Site).filter(Site.code == code).first():
        return
    db.add(Site(code=code, name=os.environ.get("PRIMARY_SITE_NAME") or code,
                active=True, sort_order=0))
    db.commit()


LABELS = {"weather": "微型氣象站", "worklog": "出工回報", "news": "職安新知"}
RETRY_WAIT_SEC = 30


def _poll(fn) -> tuple:
    """回傳 (是否成功, 訊息, 是否為暫時性失敗)。缺設定（sys.exit）也算失敗——
    該塊資料就是拿不到，而且不會自己好。"""
    try:
        return True, bs.redact(fn()), False
    except SystemExit as e:
        return False, bs.redact(f"略過（{e}）"), False
    except Exception as e:             # noqa: BLE001
        return False, bs.redact(f"失敗（{type(e).__name__}: {e}）"), bs.is_transient(e)


def _run_collectors() -> dict:
    """依序 poll 三支收集程式，暫時性失敗的等 30 秒重試一次。
    回傳 {名稱: (是否成功, 訊息, 是否為暫時性失敗)}。

    收集程式內部 log() 印到 stdout，會污染要輸出的 JSON——收集期間把
    stdout 導到 stderr。"""
    import contextlib

    from collectors import osha_news, weather, worklog
    fns = {"weather": weather.poll_once, "worklog": worklog.poll_once,
           "news": osha_news.poll_once}
    with contextlib.redirect_stdout(sys.stderr):
        result = {name: _poll(fn) for name, fn in fns.items()}
        retry = [n for n, (good, _, transient) in result.items() if not good and transient]
        if retry:
            print(f"暫時連不上：{', '.join(retry)}，{RETRY_WAIT_SEC} 秒後重試", file=sys.stderr)
            time.sleep(RETRY_WAIT_SEC)
            for name in retry:
                result[name] = _poll(fns[name])
    return result


def _read_external(db, ok: dict) -> dict:
    """組看板外部區塊；只放成功的收集程式負責的區塊（weather→station、
    worklog→worklog/hazards/daily_totals、news→news），失敗的整組不放。
    組裝邏輯與地端 board_data 共用（app.main 的 *_section），欄位一致。"""
    from app.main import (environment_snapshot, hazards_section, news_section,
                          worklog_section)

    today = date.today()
    out = {"schema": 2,
           "generated_at": datetime.now().isoformat(timespec="seconds"),
           "failed": sorted(n for n, good in ok.items() if not good)}

    if ok.get("weather"):
        primary = (os.environ.get("PRIMARY_SITE_CODE") or "").strip()
        env_all = environment_snapshot(db)
        out["station"] = next((s for s in env_all if s.get("site_code") == primary),
                              env_all[0] if env_all else None)

    if ok.get("worklog"):
        wl, out["worklog"] = worklog_section(db, today)
        out["hazards"] = hazards_section(wl)          # 雲端沒有列控表
        # 每日出工總數：暫存庫每次清空，無法自己累積歷史——把試算表現有的
        # 各日總數送上雲端，由雲端逐日合併保存，上月人日在雲端算。
        since = today - timedelta(days=HISTORY_DAYS)
        totals: dict = {}
        for w in (db.query(WorkLog)
                  .filter(WorkLog.report_date >= since,
                          WorkLog.report_date <= today).all()):
            k = w.report_date.isoformat()
            totals[k] = totals.get(k, 0) + (w.headcount or 0)
        out["daily_totals"] = totals

    if ok.get("news"):
        out["news"] = news_section(db)
    return out


def _push(payload: dict) -> bool:
    """推上雲端。成功回 True；雲端暫時連不上回 False（不寄信，下一輪再推）；
    權杖錯等需要處理的失敗直接結束（非 0）。"""
    url = (os.environ.get("CLOUD_INGEST_URL") or "").strip()
    token = (os.environ.get("CLOUD_INGEST_TOKEN") or "").strip()
    if not url or not token:
        bs.annotate("error", "工地看板：未設定推送目的地", "需設定 CLOUD_INGEST_URL 與 CLOUD_INGEST_TOKEN")
        sys.exit(1)
    try:
        r = requests.post(url, json=payload,
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:                 # noqa: BLE001
        msg = f"推送失敗（{type(e).__name__}: {e}）"
        if isinstance(e, requests.HTTPError):
            msg += f"：{e.response.text[:200]}"
        if bs.is_transient(e):
            bs.annotate("warning", "工地看板：雲端暫時連不上", msg + "（下一輪再推）")
            return False
        bs.annotate("error", "工地看板：推送被拒", msg)
        sys.exit(1)
    print(f"已推送 external 快照（{len(json.dumps(payload))} bytes）")
    return True


def main() -> int:
    bs.utf8_stdio()
    bs.check_timezone()
    load_env()                 # 有 .env 就讀（setdefault，不覆寫上面釘住的值）
    try:
        init_db()
        db = SessionLocal()
        try:
            _seed_primary_site(db)
            results = _run_collectors()
            for name, (good, msg, _) in results.items():
                print(f"[collector] {name}: {msg}", file=sys.stderr)
            ok = {n: good for n, (good, _, _) in results.items()}
            payload = _read_external(db, ok)
        finally:
            db.close()
    finally:
        bs.remove_tmp_db()

    # 失敗的區塊寫成註記（摘要頁與通知信看得到）。暫時性的只留黃色註記，
    # 需要處理的轉紅寄信。
    needs_fix = False
    for name, (good, msg, transient) in results.items():
        if good:
            continue
        if transient:
            bs.annotate("warning", f"工地看板：{LABELS[name]}暫時連不上",
                        f"{msg}（已重試一次；這塊沿用上一份資料，下一輪再抓）")
        else:
            needs_fix = True
            bs.annotate("error", f"工地看板：{LABELS[name]}需要處理", msg)

    if not any(ok.values()):
        print("三支收集程式全部失敗，不推送（雲端保留上一份資料）", file=sys.stderr)
        return 1 if needs_fix else 0

    if "--push" in sys.argv:
        _push(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))

    if payload["failed"]:
        print(f"部分收集失敗：{', '.join(payload['failed'])}（這些區塊沿用雲端"
              f"上一份資料，看板過期時會亮警示）", file=sys.stderr)
    return 1 if needs_fix else 0


if __name__ == "__main__":
    sys.exit(main())
