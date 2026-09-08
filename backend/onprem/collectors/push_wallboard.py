"""把整份儀表板資料推到雲端，供工地電腦唯讀顯示。

為什麼需要這支
--------------
讓「只有一個瀏覽器」的電腦也看得到儀表板：地端把整份資料推到雲端存成
快照，任何能上網的裝置開網址就能唯讀顯示。

做法沿用早期「推監視畫面上雲」的模式：由這台在公司網路內、且連得到
網際網路的機器主動往外送，工地電腦只負責開網址。地端不需要對外開任何埠。

**這是備援，預設關閉。** 工地看儀表板的正規做法是「工地檢視器」安裝包
（docs/工地檢視器.md），完全不經雲端；只在檢視器暫時無法交付時，
把 WALL_ENABLED 設為 true 啟用這條路。

推什麼
------
打本機 API 拿三樣東西打包成一份快照：

    branding   版頭名稱與主場站
    sites      工地清單（下拉選單用）
    dashboard  戰情室彙總（缺失、環境、人數、熱危害、重點工項、進度）
    board      工地看板五區塊（主場站；供雲端唯讀顯示新版看板）

快照是**一份固定的視圖**：天數與工地範圍在這裡就決定了，工地電腦上的
下拉選單不會作用（前端在看板模式會隱藏它們）。牆上看板不需要互動。

**監視畫面不在快照內**，也推不上去——CCTV 是即時代理到工地攝影機的，
雲端碰不到。前端在看板模式讓監視格整個不顯示（刻意的，牆上位置很貴，
不擺一句永遠不變的說明文字）。

設定（.env.onprem）
-------------------
    WALL_ENABLED      設 true 才會推送（本程式是備援，預設 false 什麼都不做）
    CLOUD_API_URL     雲端站台網址，例如 https://xxx.netlify.app
    CLOUD_SYNC_TOKEN  推送權杖，需與雲端的 SITE_AGENT_TOKEN 相同
                      （與 sync_forms 共用同一組，不另外設）
    WALL_DAYS         快照涵蓋幾天的統計，預設 30
    WALL_SITE_ID      只看單一工地時填其 id；留空＝全部工地
    WALL_INTERVAL     每輪間隔秒數，預設 900（15 分，與前端輪詢對齊）
    WALL_HOURS        推送時段，預設 6-20；時段外不推（牆前沒有人）
    CLOUD_DAILY_BUDGET 每台機器每日雲端呼叫上限，預設 300（見 cloud_budget.py）
    ONPREM_API_URL    本機 API 位址，預設 http://127.0.0.1:8000

用法（在 backend/onprem 目錄下執行）
------------------------------------
    python -m collectors.push_wallboard            # 跑一輪就結束，用來驗證
    python -m collectors.push_wallboard --loop     # 持續執行

一輪失敗只記錄並繼續，不整支停掉——工地沒有人會去看它還活著沒有。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from .cloud_budget import spend, status as budget_status
from .config import env, load_env, log

TIMEOUT = 30
SCHEMA = 1

# 上次推上去的內容指紋。內容沒變就不重推——夜間與假日常常整段時間
# 數字都一樣，重推只是把同一份資料再寫一次，白花一次呼叫。
DIGEST_FILE = None          # 延後到第一次用時才決定（要等 app.db 載入）


def _digest_path() -> Path:
    global DIGEST_FILE
    if DIGEST_FILE is None:
        from app.db import BASE_DIR
        DIGEST_FILE = Path(BASE_DIR) / "wallboard_digest.txt"
    return DIGEST_FILE


def _digest(snap: dict) -> str:
    """算內容指紋，排除每次必變的時間戳。

    generated_at 每一輪都不同，不排除的話「內容有沒有變」永遠是有變，
    這個最佳化就完全失效了。dashboard 與 board 各自帶一個，都要排除。
    """
    def _strip(v):
        if isinstance(v, dict):
            v = dict(v)
            v.pop("generated_at", None)
        return v
    payload = {"branding": snap.get("branding"), "sites": snap.get("sites"),
               "dashboard": _strip(snap.get("dashboard")),
               "board": _strip(snap.get("board"))}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _last_digest() -> str:
    try:
        return _digest_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_digest(d: str) -> None:
    try:
        _digest_path().write_text(d, encoding="utf-8")
    except OSError as e:                                # noqa: BLE001
        log(f"無法記錄內容指紋（{e}），下一輪會重推一次")


def local(path: str) -> dict | list:
    """讀本機 API。伺服器沒起來時這裡會失敗，訊息要讓人看得懂。"""
    base = env("ONPREM_API_URL", "http://127.0.0.1:8000").rstrip("/")
    r = requests.get(f"{base}{path}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def build_snapshot() -> dict:
    days = env("WALL_DAYS", "30") or "30"
    site_id = env("WALL_SITE_ID")
    q = f"/api/dashboard?days={days}" + (f"&site_id={site_id}" if site_id else "")

    # 工地清單直接讀資料庫。/api/sites 要登入而這支程式沒有 session，
    # 之前改成「取不到就空著」——但那讓快照永遠帶一個空陣列，形同虛設：
    # 哪天有人把工地下拉打開、或寫了依賴它的功能，就會拿到空清單。
    # 這支程式本來就跑在地端、本來就連得到資料庫，沒有理由繞 HTTP。
    from app.db import Site, SessionLocal
    db = SessionLocal()
    try:
        sites = [{"id": s.id, "code": s.code, "name": s.name,
                  "department": s.department, "active": bool(s.active)}
                 for s in db.query(Site).order_by(Site.sort_order).all()]
    finally:
        db.close()

    branding = local("/api/branding")

    # 工地看板（五區塊）的整份資料，供雲端唯讀顯示新版看板。取主場站
    # 一份——環境與出工本來就以主場站為準。抓不到就不放，快照照樣有
    # 戰情室那份 dashboard。
    primary_code = (branding.get("primary_site_code") or "").strip()
    primary = next((s for s in sites if s["code"] == primary_code),
                   sites[0] if sites else None)
    board = None
    if primary:
        try:
            board = local(f"/api/board-data/{primary['id']}")
        except Exception as e:                          # noqa: BLE001
            log(f"（提醒）取工地看板資料失敗，快照這次不含 board：{e}")

    return {
        "schema": SCHEMA,
        "branding": branding,
        "sites": sites,
        "dashboard": local(q),
        "board": board,
    }


def push_once() -> str:
    # 預設關閉：工地看儀表板的正規做法是安裝包直連中央資料庫
    # （見 docs/工地檢視器.md），雲端看板只是它連不上時的備援。
    # CLOUD_API_URL 與 CLOUD_SYNC_TOKEN 因表單同步一定有值，
    # 不能拿它們當開關，要另設 WALL_ENABLED。
    if (env("WALL_ENABLED", "false") or "").lower() != "true":
        return "WALL_ENABLED 未開啟，已跳過（雲端看板為備援，預設不推）"

    # 只在上班時段推。牆上沒有人的時候把快照推上去，是把額度花在
    # 沒有人會看的畫面上——夜間與假日佔掉一天的四成以上。
    hours = env("WALL_HOURS", "6-20")
    try:
        start_h, end_h = (int(x) for x in hours.split("-", 1))
        now_h = datetime.now().hour
        if not (start_h <= now_h < end_h):
            return f"目前非看板時段（{hours} 時），已跳過"
    except ValueError:
        log(f"WALL_HOURS 格式錯誤（{hours}），本輪不做時段限制")

    base = env("CLOUD_API_URL").rstrip("/")
    token = env("CLOUD_SYNC_TOKEN")
    if not base:
        return "未設定 CLOUD_API_URL，已跳過"
    if not token:
        return "未設定 CLOUD_SYNC_TOKEN，已跳過（需與雲端 SITE_AGENT_TOKEN 相同）"

    snap = build_snapshot()
    body = json.dumps(snap, ensure_ascii=False, default=str)

    # 內容沒變就不要推。收集程式在夜間、假日常常抓到一模一樣的數字，
    # 推上去只是把同一份資料重寫一次，白白花掉一次呼叫。
    # generated_at 每次都不同，比對時要排除，否則永遠「有變動」。
    digest = _digest(snap)
    if digest == _last_digest():
        return "內容與上次相同，已跳過（省一次雲端呼叫）"

    # 走每日硬上限。這是上次額度被燒光後加的保護：不論上游怎麼壞，
    # 這台機器每天最多只會打固定次數。
    if not spend("wallboard"):
        return f"今日雲端呼叫已達上限，已跳過（{budget_status()}）"

    r = requests.post(
        f"{base}/api/v1/ingest/wallboard",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-Agent-Token": token},
        timeout=TIMEOUT,
    )
    if r.status_code == 401:
        return "權杖驗證失敗：CLOUD_SYNC_TOKEN 與雲端的 SITE_AGENT_TOKEN 不一致"
    r.raise_for_status()

    _save_digest(digest)
    d = snap["dashboard"]
    kpi = d.get("kpi") or {}
    # 用位元組數不用字元數：這份 JSON 以中文為主，UTF-8 一字三位元組，
    # len(str) 會把實際傳輸量少報三倍
    return (f"已推送 {len(body.encode('utf-8')) / 1024:.0f} KB"
            f"（工地 {len(snap['sites'])} 處、缺失統計 {kpi.get('open', '?')} 件未結）")


def main() -> None:
    load_env()
    interval = int(env("WALL_INTERVAL", "900") or 900)
    loop = "--loop" in sys.argv
    while True:
        try:
            log(push_once())
        except requests.RequestException as e:
            # 本機伺服器還沒起來、或對外連線斷了。兩者都會自己恢復，
            # 不必讓整支程式死掉。
            log(f"本輪失敗：{e}")
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
