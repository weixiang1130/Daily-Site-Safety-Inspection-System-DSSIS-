"""工地檢視器的單一進入點：一個程序＝儀表板伺服器＋三支收集程式。

為什麼要有這一支
----------------
工地檢視器（見 docs/工地檢視器.md）是一個免安裝資料夾，跑在工地的普通
電腦上——**沒有公司資料庫權限、沒有管理員權限，只有網際網路**。
它需要的資料恰好全部在外網：

    表單／缺失   雲端填報站 /api/v1/export
    環境數據     氣象站廠商平台
    在場人數     人臉辨識裝置（公網 IP）
    重點工項     列控表（打包時已烤進本機 SQLite，改版時匯入小檔更新）

在中央主機上，伺服器與收集程式各自掛工作排程；工地電腦沒有人會去維護
排程，越少活動零件越好。因此這支把全部東西收進**一個程序**：
收集程式各開一條執行緒輪詢，主執行緒跑 uvicorn。點一個檔就全亮，
關一個視窗就全滅，現場的人不需要理解「伺服器」與「收集程式」的差別。

設定沿用 `連線設定.env`（見 app/envfile.py）。哪些收集程式會動，
取決於對應設定有沒有填——沒填的那支只會安靜跳過，不會報錯。

    VIEWER_PORT   本機埠，預設 8000
"""

from __future__ import annotations

import os
import threading
import time

from app.envfile import load_env

load_env()          # 必須在 import app.main 之前——它在模組層讀環境變數

from app.db import init_db          # noqa: E402
from collectors import face, sync_forms, weather  # noqa: E402
from collectors.config import env, log  # noqa: E402


def loop_simple(name: str, poll, interval_key: str, default: int) -> None:
    """一般收集程式的輪詢：跑一次、記錄、睡到下一輪。"""
    interval = int(env(interval_key, str(default)) or default)
    while True:
        try:
            log(f"[{name}] {poll()}")
        except Exception as e:                          # noqa: BLE001
            log(f"[{name}] 本輪失敗：{e}")
        time.sleep(interval)


def loop_sync() -> None:
    """表單同步：首輪全量（本機還沒有任何表單資料時），之後增量。"""
    # 預設值統一由 sync_forms 定義，兩個進入點不各寫一個數字
    interval = int(env("SYNC_INTERVAL", str(sync_forms.DEFAULT_INTERVAL))
                   or sync_forms.DEFAULT_INTERVAL)
    full = not os.path.exists(os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "sync_state.json"))
    while True:
        try:
            more = True
            while more:
                msg, more = sync_forms.sync_once(full=full)
                full = False
                log(f"[sync] {msg}")
        except Exception as e:                          # noqa: BLE001
            log(f"[sync] 本輪失敗：{e}")
        time.sleep(interval)


def main() -> None:
    init_db()   # 本機 SQLite：打包時已含結構與工項，這裡只補缺

    jobs = [
        threading.Thread(target=loop_sync, name="sync", daemon=True),
        threading.Thread(target=loop_simple, name="weather",
                         args=("weather", weather.poll_once,
                               "WEATHER_INTERVAL", 900), daemon=True),
        threading.Thread(target=loop_simple, name="face",
                         args=("face", face.poll_once,
                               "FACE_INTERVAL", 300), daemon=True),
    ]
    for j in jobs:
        j.start()

    import uvicorn
    from app.main import app
    port = int(env("VIEWER_PORT", "8000") or 8000)
    # 只聽本機：這台電腦自己看，不對外服務
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
