"""雲端呼叫的每日硬上限——讓任何故障都不可能燒光額度。

為什麼需要這個
--------------
2026-09-03 免費額度被燒光、整個站台回 503 usage_exceeded，工地連填報都
打不開。事後推算：工地檢視器的啟動檔在程式當掉後每 10 秒重啟一次，而
同步程式在每次啟動時無條件呼叫一次雲端——兩者相乘就是每 13 秒一次、
約 20 萬次／月，是整站正常用量的十倍以上。

真正的教訓不是「頻率設錯」，而是**用量沒有上限保證**：只要有任何一個
迴圈失控，它就能無限打下去，而我們要等到站台掛掉才會發現。

這支程式提供的保證很簡單：**不論上游怎麼壞，這台機器每天最多只會打
CLOUD_DAILY_BUDGET 次雲端。** 計數存在磁碟，所以重啟也繞不過去——
這正是上次那個故障能無限重來的原因。

用法
----
    from .cloud_budget import spend
    if not spend("wallboard"):
        return "今日雲端呼叫已達上限，跳過"
    ...真正去打雲端...

設定（.env.onprem）
-------------------
    CLOUD_DAILY_BUDGET  每台機器每日上限，預設 300。
                        正常用量：中央主機約 58 次／日、工地檢視器約 2 次／日，
                        300 已有五倍餘裕；而就算完全失控，一台機器一個月也
                        只會用掉 9,000 次（免費額度 125,000 的 7%）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.db import BASE_DIR

from .config import env, log

# 與 sync_state.json 放一起：都是這台機器自己的執行時狀態，不是業務資料
BUDGET_FILE = Path(BASE_DIR) / "cloud_budget.json"
DEFAULT_BUDGET = 300


def _limit() -> int:
    try:
        return max(1, int(env("CLOUD_DAILY_BUDGET", str(DEFAULT_BUDGET))
                          or DEFAULT_BUDGET))
    except ValueError:
        return DEFAULT_BUDGET


def _load() -> dict:
    try:
        d = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # 跨日自動歸零。用日期字串比對而不是存時間戳算差值——
    # 機器可能整夜關機，「過了 24 小時」與「換了一天」不是同一件事。
    return d if d.get("day") == date.today().isoformat() else {}


def spend(what: str, n: int = 1) -> bool:
    """要求 n 次呼叫額度。超過上限回 False，呼叫端必須跳過而不是硬打。"""
    limit = _limit()
    d = _load()
    used = int(d.get("used", 0))
    if used + n > limit:
        # 只在剛好跨過門檻時寫一次檔並記一次 log。
        # 失控迴圈每秒可能來幾十次，若每次都寫檔，這個保護機制自己就會
        # 變成猛烈的磁碟寫入——正好在它該保護的那個場景裡。
        if used <= limit:
            log(f"[budget] 今日雲端呼叫已達上限 {limit} 次，{what} 起暫停到明天。"
                f"這是保護機制：正常用量遠低於此，會觸發代表有東西在重複呼叫。")
            _save(limit + 1)          # 記成「已超過」，不再累加
        return False
    _save(used + n)
    return True


def _save(used: int) -> None:
    try:
        BUDGET_FILE.write_text(
            json.dumps({"day": date.today().isoformat(), "used": used}),
            encoding="utf-8")
    except OSError as e:                                # noqa: BLE001
        log(f"[budget] 無法寫入用量檔（{e}），本次不計數")


def status() -> str:
    d = _load()
    used, limit = int(d.get("used", 0)), _limit()
    return (f"已達上限 {limit} 次（今日）" if used > limit
            else f"{used}/{limit} 次（今日）")
