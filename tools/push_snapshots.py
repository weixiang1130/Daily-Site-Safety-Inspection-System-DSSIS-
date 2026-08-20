"""把監視器畫面從公司網路推送到戰情室。

為什麼需要這支程式
------------------
戰情室跑在 Netlify（美東），而監視器主機對來源 IP 有限制：同一時刻由公司
網路取像正常（401 挑戰 → 200），由 Netlify 卻在還沒帶帳密的第一次請求就被
回 403。Netlify 函式沒有固定的對外 IP 可以加白名單，因此雲端直連這條路走不通。

改為由這支程式在**公司網路內**定時取像，再 POST 回戰情室。畫面存在我們自己
的儲存區，儀表板只讀我們的資料——這也是本專案一貫的做法：不受制於對方的
存取限制。

跑在哪裡
--------
任何一台**在公司網路內、且連得到網際網路**的機器：工地的填報用電腦、
辦公室的值班電腦都可以。它只需要出去的連線，不需要對外開任何埠。

設定（環境變數）
----------------
    CAM_URL        監視器位址，例如 http://主機:81
    CAM_USER       監視器帳號
    CAM_PASS       監視器密碼
    CAM_CHANNELS   要推送的頻道，逗號分隔，例如 4,7
    WAR_ROOM_URL   戰情室網址，例如 https://xxx.netlify.app
    AGENT_TOKEN    推送權杖，需與站台的 SITE_AGENT_TOKEN 相同
    PUSH_INTERVAL  每輪間隔秒數，預設 60

用法
----
    python tools/push_snapshots.py

要長期執行，用「工作排程器」設成開機自動啟動即可；程式本身會一直跑，
遇到單次失敗會記錄並繼續，不會整個停掉——工地沒有人會去看它有沒有活著。

按 Ctrl+C 結束。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


CAM_URL = env("CAM_URL").rstrip("/")
CAM_USER = env("CAM_USER")
CAM_PASS = env("CAM_PASS")
CHANNELS = [c.strip() for c in env("CAM_CHANNELS", "1").split(",") if c.strip()]
WAR_ROOM_URL = env("WAR_ROOM_URL").rstrip("/")
AGENT_TOKEN = env("AGENT_TOKEN")
INTERVAL = int(env("PUSH_INTERVAL", "60") or 60)

# 單次請求的上限。取像約 380 KB，正常一兩秒內完成；
# 設上限是為了避免主機沒回應時整支程式卡住不動。
TIMEOUT = 20


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def check_config() -> None:
    missing = [k for k, v in {
        "CAM_URL": CAM_URL, "CAM_USER": CAM_USER, "CAM_PASS": CAM_PASS,
        "WAR_ROOM_URL": WAR_ROOM_URL, "AGENT_TOKEN": AGENT_TOKEN,
    }.items() if not v]
    if missing:
        sys.exit("缺少環境變數：" + "、".join(missing) + "\n詳見本檔開頭的說明。")


def grab(channel: str) -> bytes:
    """從監視器取一張畫面。"""
    r = requests.get(
        f"{CAM_URL}/cgi-bin/snapshot.cgi",
        params={"channel": channel},
        auth=HTTPDigestAuth(CAM_USER, CAM_PASS),
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        # 有些機型驗證失敗仍回 200，內容卻是錯誤訊息。
        # 照推上去的話，牆上只會出現一片空白而查不出原因。
        raise RuntimeError(f"回傳非影像內容（{ctype}）：{r.content[:80]!r}")
    if len(r.content) < 1024:
        raise RuntimeError(f"影像過小（{len(r.content)} 位元組）")
    return r.content


def push(channel: str, data: bytes) -> None:
    """把畫面送到戰情室。"""
    r = requests.post(
        f"{WAR_ROOM_URL}/api/v1/ingest/snapshot",
        params={"channel": channel},
        headers={"Content-Type": "image/jpeg", "X-Agent-Token": AGENT_TOKEN},
        data=data,
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}：{r.text[:120]}")


def main() -> None:
    check_config()
    log(f"開始推送：頻道 {'、'.join(CHANNELS)}，每 {INTERVAL} 秒一輪")

    while True:
        for ch in CHANNELS:
            try:
                data = grab(ch)
                push(ch, data)
                log(f"頻道 {ch} 已推送 {len(data) // 1024} KB")
            except Exception as e:
                # 單一頻道失敗不影響其他頻道，也不中斷整個迴圈——
                # 工地不會有人盯著這支程式，它必須自己撐下去。
                log(f"頻道 {ch} 失敗：{e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已停止")
