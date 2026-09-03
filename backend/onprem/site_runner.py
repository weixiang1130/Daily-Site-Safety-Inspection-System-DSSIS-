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


def open_browser_when_ready(port: int, timeout_sec: int = 120) -> None:
    """等伺服器真的在聽了，才開瀏覽器。

    這件事以前由啟動用的 .cmd 拿 powershell 做，但那裡把埠寫死成 8000，
    使用者一改 VIEWER_PORT 就會等錯埠、開錯網址。由這支程式做才拿得到
    真正的埠號——它本來就是決定埠號的人。

    冷開機的工地電腦（防毒逐一掃描 2795 個檔案）可能要 20~60 秒才起得來，
    太早開瀏覽器只會顯示「無法連線」，讓人以為壞了。
    """
    import socket
    import webbrowser
    url = f"http://127.0.0.1:{port}/static/dashboard.html"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.8)
    log(f"等了 {timeout_sec} 秒仍未聽到 {port} 埠，未自動開啟瀏覽器；"
        f"伺服器起來後請手動開 {url}")


def main() -> None:
    # 讓編譯型套件（FastAPI 依賴的 pydantic_core 等）找得到包內自帶的
    # VC++ runtime。Python 3.8+ 載入延伸模組時，不會自動把 python.exe
    # 所在目錄納入 DLL 搜尋——乾淨的辦公電腦上，這會讓網頁框架的元件
    # 載入失敗，而且不一定留下 Python 錯誤（行程直接死）。
    try:
        pydir = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "python"))
        if hasattr(os, "add_dll_directory") and os.path.isdir(pydir):
            os.add_dll_directory(pydir)
    except Exception as e:                              # noqa: BLE001
        log(f"（提醒）加入 DLL 搜尋路徑失敗，可忽略：{e}")

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

    port = int(env("VIEWER_PORT", "8000") or 8000)
    # 逐步記錄，且任何失敗都強制寫進 log——網頁伺服器起不來時，這幾行
    # 是唯一能看出「死在哪一步」的線索（headless 機器沒人看得到主控台）。
    log("收集程式已啟動，正在載入網頁伺服器模組…")
    try:
        import uvicorn
        from app.main import app
    except (KeyboardInterrupt, SystemExit):
        raise                                           # 正常關閉，不是故障
    except Exception as e:                              # noqa: BLE001
        import traceback
        log("網頁伺服器模組載入失敗：" + repr(e))
        log(traceback.format_exc())
        raise
    log(f"模組載入完成，開始服務 http://127.0.0.1:{port}")
    threading.Thread(target=open_browser_when_ready, args=(port,),
                     name="browser", daemon=True).start()
    try:
        # 只聽本機：這台電腦自己看，不對外服務
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except (KeyboardInterrupt, SystemExit):
        # 使用者自己按 Ctrl+C 或關視窗——這是正常關閉，不是故障。
        # 記成「啟動失敗」會在日後查 log 時製造假線索。
        log("已停止")
        raise
    except Exception as e:                              # noqa: BLE001
        import traceback
        log("網頁伺服器啟動失敗：" + repr(e))
        log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
