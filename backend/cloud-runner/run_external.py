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

用法
----
    python backend/cloud-runner/run_external.py            # 跑一次、印出（本機驗證）
    python backend/cloud-runner/run_external.py --push     # 跑一次並推上雲端
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime

# 收集程式與模型都在 backend/onprem 底下；把它加進 import 路徑。
ONPREM = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "onprem"))
sys.path.insert(0, ONPREM)

# 一定要在 import app.db 之前決定資料庫後端：用暫存 SQLite，絕不連正式庫。
# 每次執行換一個檔名，跑完即棄——這台機器不保留任何狀態。
_TMP_DB = os.path.join(tempfile.gettempdir(),
                       f"wallboard-external-{os.getpid()}.db")
os.environ["DB_BACKEND"] = "sqlite"
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import NewsItem, Site, SessionLocal, WorkLog, init_db  # noqa: E402
from app.envfile import load_env  # noqa: E402

TIMEOUT = 30


def _seed_primary_site(db) -> None:
    """氣象站收集把測站對到工地代碼；暫存庫是空的，先塞一筆主場站，
    讓 environment_snapshot 能把 site_code 對回工地名（對不到只是少了
    工地名，不影響數值）。棟別不細分，看板環境本來就以主場站為準。"""
    code = (os.environ.get("PRIMARY_SITE_CODE") or "").strip()
    if not code or db.query(Site).filter(Site.code == code).first():
        return
    db.add(Site(code=code, name=os.environ.get("PRIMARY_SITE_NAME") or code,
                active=True, sort_order=0))
    db.commit()


def _run_collectors() -> list:
    """依序 poll 三支收集程式，回傳每支的結果訊息（給 log 看）。
    任何一支失敗都不該讓另外兩支跟著沒有——各自 try，壞的那塊留空。

    收集程式內部 log() 印到 stdout，會污染我們要輸出的 JSON——收集期間
    把 stdout 導到 stderr，讓 stdout 只留最後那份乾淨資料。"""
    import contextlib

    from collectors import osha_news, weather, worklog
    msgs = []
    with contextlib.redirect_stdout(sys.stderr):
        for name, fn in (("weather", weather.poll_once),
                         ("worklog", worklog.poll_once),
                         ("news", osha_news.poll_once)):
            try:
                msgs.append(f"{name}: {fn()}")
            except SystemExit as e:        # 收集程式對缺設定會 sys.exit
                msgs.append(f"{name}: 略過（{e}）")
            except Exception as e:         # noqa: BLE001
                msgs.append(f"{name}: 失敗（{e}）")
    return msgs


def _read_external(db) -> dict:
    """把三塊外部資料讀成看板 board-data 相容的形狀。
    station/worklog/news 的欄位與 app.main.board_data 對齊，雲端合併時
    才不用再轉換。"""
    from app.main import environment_snapshot

    primary = (os.environ.get("PRIMARY_SITE_CODE") or "").strip()
    env_all = environment_snapshot(db)
    station = next((s for s in env_all if s.get("site_code") == primary),
                   env_all[0] if env_all else None)

    today = date.today()
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

    news = [{"title": n.title, "url": n.url,
             "published": n.published.isoformat() if n.published else None}
            for n in db.query(NewsItem)
            .order_by(NewsItem.published.desc(), NewsItem.id.desc()).limit(8).all()]

    return {
        "schema": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "station": station,
        "worklog": worklog,
        "news": news,
    }


def _push(payload: dict) -> None:
    url = (os.environ.get("CLOUD_INGEST_URL") or "").strip()
    token = (os.environ.get("CLOUD_INGEST_TOKEN") or "").strip()
    if not url or not token:
        sys.exit("要 --push 需設定 CLOUD_INGEST_URL 與 CLOUD_INGEST_TOKEN")
    r = requests.post(url, json=payload,
                      headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    if not r.ok:
        sys.exit(f"推送失敗（HTTP {r.status_code}）：{r.text[:200]}")
    print(f"已推送 external 快照（{len(json.dumps(payload))} bytes）")


def main() -> None:
    # 輸出一律 UTF-8：GitHub Actions（Linux）本來就是，但 Windows 本機驗證
    # 時 stdout 預設 cp950（Big5），遇到看板裡的中文（如「黃」）會整支炸掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    load_env()                 # 有 .env 就讀（本機驗證方便）；Actions 走環境變數
    init_db()
    db = SessionLocal()
    try:
        _seed_primary_site(db)
        for m in _run_collectors():
            print("[collector]", m, file=sys.stderr)
        payload = _read_external(db)
    finally:
        db.close()

    if "--push" in sys.argv:
        _push(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))

    try:
        os.remove(_TMP_DB)     # 用完即棄，不留狀態
    except OSError:
        pass


if __name__ == "__main__":
    main()
