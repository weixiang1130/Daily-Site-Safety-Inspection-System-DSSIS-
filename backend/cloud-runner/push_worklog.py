# -*- coding: utf-8 -*-
"""出工資料庫推送：把出工回報的歷史明細寫進雲端資料庫，供人力與工種分析。

看板只顯示「今天」，歷史原本只存在來源試算表裡——它曾被移到垃圾桶，若被
截斷或換表，歷史就沒了。這支把解析結果寫進雲端資料庫 worklog_* 表，成為
出工歷史的正本（表結構與設計見 netlify/database/migrations/008_worklog）。

排程
----
GitHub Actions 每天 00:07（台北）那一輪跑一次，重送最近 14 天：事後修改的
回報會被更新，某晚沒跑到也由隔晚補上。只在半夜寫一次，是因為雲端資料庫
按「醒著的時數」計費——這個時段看板每日第一次讀取本來就會喚醒資料庫。

解析與本機看板共用 collectors/worklog.py 的 collect_reports()，數字一致。

用法
----
    python backend/cloud-runner/push_worklog.py --dry-run      # 只解析、印摘要
    python backend/cloud-runner/push_worklog.py                # 推最近 14 天
    python backend/cloud-runner/push_worklog.py --all          # 首次：推全部歷史

需要的環境變數：WORKLOG_SHEET_URL、BUILDING_LABELS、CLOUD_INGEST_URL
（會自動改用同層的 /worklog 端點）、CLOUD_INGEST_TOKEN。
"""
from __future__ import annotations

import _bootstrap as bs  # noqa: I001 —— 必須最先：時區、暫存 SQLite、import 路徑

import argparse
import json
import os
import sys
from datetime import date, timedelta

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.envfile import load_env  # noqa: E402

TIMEOUT = 60
CHUNK = 300                 # 每批筆數（雲端單次上限 1000）


def _iso(v):
    return v.isoformat(timespec="seconds") if hasattr(v, "hour") else (
        v.isoformat() if v else None)


def _report(item: dict) -> dict:
    return {
        "report_date": item["report_date"].isoformat(),
        "building": item["building"] or "",
        "vendor": item["vendor"],
        "headcount": item["headcount"],
        "trade_summary": item["trade"],
        "supervisor": item["supervisor"],
        "tasks": item["tasks"],
        "reporter": item["reporter"],
        "reported_at": _iso(item["reported_at"]),
        "message_id": item["message_id"],
        "raw": item["raw"],
        "trades": item.get("trades") or [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="出工資料庫推送")
    ap.add_argument("--days", type=int, default=14, help="重送最近幾天（預設 14）")
    ap.add_argument("--all", action="store_true", help="推送試算表內全部歷史（首次建庫用）")
    ap.add_argument("--dry-run", action="store_true", help="只解析並印出摘要，不推送")
    args = ap.parse_args()

    bs.utf8_stdio()
    bs.check_timezone()
    load_env()
    try:
        from collectors.worklog import collect_reports
        try:
            row_count, parsed, rejects = collect_reports()
        except Exception as e:                           # noqa: BLE001
            print(bs.redact(f"讀取出工回報失敗（{type(e).__name__}: {e}）"), file=sys.stderr)
            return 1
    finally:
        bs.remove_tmp_db()

    since = None if args.all else date.today() - timedelta(days=args.days)
    reports = [_report(i) for i in parsed.values()
               if since is None or i["report_date"] >= since]
    rejects = [{"message_id": x["message_id"], "reported_at": _iso(x["reported_at"]),
                "reporter": x["reporter"], "raw": x["raw"]}
               for x in rejects
               if x["message_id"] and (since is None or x["reported_at"] is None
                                       or x["reported_at"].date() >= since)]
    with_trades = sum(1 for r in reports if r["trades"])
    scope = "全部歷史" if since is None else f"{since} 起"
    print(f"試算表 {row_count} 列｜{scope}：回報 {len(reports)} 筆（含工種明細 {with_trades}）"
          f"、解析失敗 {len(rejects)} 則")

    if args.dry_run:
        return 0

    url = bs.cloud_endpoint("worklog")
    token = (os.environ.get("CLOUD_INGEST_TOKEN") or "").strip()
    if not url or not token:
        print("需設定 CLOUD_INGEST_URL 與 CLOUD_INGEST_TOKEN", file=sys.stderr)
        return 1

    batches = [reports[i:i + CHUNK] for i in range(0, len(reports), CHUNK)] or [[]]
    done = 0
    for n, batch in enumerate(batches):
        body = {"reports": batch, "rejects": rejects if n == 0 else []}
        try:
            r = requests.post(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                              timeout=TIMEOUT)
        except requests.RequestException as e:
            print(bs.redact(f"推送失敗（{type(e).__name__}: {e}）"), file=sys.stderr)
            return 1
        if not r.ok:
            print(bs.redact(f"推送失敗（HTTP {r.status_code}）：{r.text[:200]}"), file=sys.stderr)
            return 1
        done += r.json().get("reports", 0)
    print(f"已寫入出工資料庫：回報 {done} 筆（{len(batches)} 批）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
