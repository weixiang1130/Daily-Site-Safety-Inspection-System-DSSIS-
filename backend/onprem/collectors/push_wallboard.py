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
    dashboard  儀表板主體（缺失、環境、人數、熱危害、重點工項、進度）

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
    WALL_INTERVAL     每輪間隔秒數，預設 300
    ONPREM_API_URL    本機 API 位址，預設 http://127.0.0.1:8000

用法（在 backend/onprem 目錄下執行）
------------------------------------
    python -m collectors.push_wallboard            # 跑一輪就結束，用來驗證
    python -m collectors.push_wallboard --loop     # 持續執行

一輪失敗只記錄並繼續，不整支停掉——工地沒有人會去看它還活著沒有。
"""

from __future__ import annotations

import json
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from .config import env, load_env, log

TIMEOUT = 30
SCHEMA = 1


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

    # 工地清單只餵下拉選單，而看板模式沒有下拉選單（快照是固定視圖）。
    # /api/sites 需要登入，這支程式沒有 session——取不到就空著，
    # 不要為了一個用不到的欄位在收集程式裡放帳密。
    try:
        sites = local("/api/sites")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            sites = []
        else:
            raise

    return {
        "schema": SCHEMA,
        "branding": local("/api/branding"),
        "sites": sites,
        "dashboard": local(q),
    }


def push_once() -> str:
    # 預設關閉：工地看儀表板的正規做法是安裝包直連中央資料庫
    # （見 docs/工地檢視器.md），雲端看板只是它連不上時的備援。
    # CLOUD_API_URL 與 CLOUD_SYNC_TOKEN 因表單同步一定有值，
    # 不能拿它們當開關，要另設 WALL_ENABLED。
    if (env("WALL_ENABLED", "false") or "").lower() != "true":
        return "WALL_ENABLED 未開啟，已跳過（雲端看板為備援，預設不推）"
    base = env("CLOUD_API_URL").rstrip("/")
    token = env("CLOUD_SYNC_TOKEN")
    if not base:
        return "未設定 CLOUD_API_URL，已跳過"
    if not token:
        return "未設定 CLOUD_SYNC_TOKEN，已跳過（需與雲端 SITE_AGENT_TOKEN 相同）"

    snap = build_snapshot()
    body = json.dumps(snap, ensure_ascii=False, default=str)

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

    d = snap["dashboard"]
    kpi = d.get("kpi") or {}
    # 用位元組數不用字元數：這份 JSON 以中文為主，UTF-8 一字三位元組，
    # len(str) 會把實際傳輸量少報三倍
    return (f"已推送 {len(body.encode('utf-8')) / 1024:.0f} KB"
            f"（工地 {len(snap['sites'])} 處、缺失統計 {kpi.get('open', '?')} 件未結）")


def main() -> None:
    load_env()
    interval = int(env("WALL_INTERVAL", "300") or 300)
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
