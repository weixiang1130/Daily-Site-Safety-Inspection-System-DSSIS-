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

多工地
------
主場站（PRIMARY_SITE_CODE）的來源是 WORKLOG_SHEET_URL；其他工地各設一個
WORKLOG_URL_<工地代碼>（見 collectors/worklog.py 的 worklog_sources）。逐工地
抓、逐工地推，**一個工地抓不到不影響其他工地**。

需要的環境變數：WORKLOG_SHEET_URL（與各 WORKLOG_URL_*）、PRIMARY_SITE_CODE、
BUILDING_LABELS、CLOUD_INGEST_URL（會自動改用同層的 /worklog 端點）、
CLOUD_INGEST_TOKEN。
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
        "site_code": item.get("site_code"),
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
        "superseded_message_ids": item.get("superseded_ids") or [],
    }


def _fail(label: str, msg: str, transient: bool) -> int:
    """暫時連不上：黃色註記、不算失敗——每晚重送最近 14 天，隔晚自然補上。
    其餘（權杖錯、來源失效、手動回補時的任何失敗）：紅色註記、算失敗。"""
    if transient:
        bs.annotate("warning", f"出工資料庫：{label}暫時連不上", msg + "（明晚重送最近 14 天會補上）")
        return 0
    bs.annotate("error", f"出工資料庫：{label}需要處理", msg)
    return 1


def _owned(reports: list) -> list:
    """這個工地本次送出的「訊息 → 鍵」完整對照（含被蓋過的舊訊息）。雲端拿它找出
    改判前留在別的鍵上的舊列；必須是整個來源的完整對照，分批時只附在最後一批。"""
    out = []
    for r in reports:
        for m in [r["message_id"], *r["superseded_message_ids"]]:
            if m:
                out.append({"site_code": r["site_code"], "report_date": r["report_date"],
                            "building": r["building"], "vendor": r["vendor"], "message_id": m})
    return out


def _push_site(code: str, url: str, args, since, endpoint: str, token: str) -> int:
    """抓一個工地、推上雲端。回傳 0（成功或暫時性失敗）或 1（需要處理）。"""
    from collectors.worklog import collect_reports, fetch_rows
    label = f"{code} "
    try:
        row_count, parsed, rejects = collect_reports(fetch_rows(url), site_code=code)
    except Exception as e:                               # noqa: BLE001
        return _fail(label, f"讀取出工回報失敗（{type(e).__name__}: {e}）",
                     bs.is_transient(e) and not args.all)

    # 出工日期或回報時間任一落在視窗內就送：廠商補報三週前的出工時，
    # 出工日期早已在視窗外，只看出工日期的話每晚都會漏掉它
    reports = [_report(i) for i in parsed.values()
               if since is None or i["report_date"] >= since
               or (i["reported_at"] is not None and i["reported_at"].date() >= since)]
    rejects = [{"site_code": code, "message_id": x["message_id"],
                "reported_at": _iso(x["reported_at"]), "reporter": x["reporter"], "raw": x["raw"]}
               for x in rejects
               if x["message_id"] and (since is None or x["reported_at"] is None
                                       or x["reported_at"].date() >= since)]
    with_trades = sum(1 for r in reports if r["trades"])
    scope = "全部歷史" if since is None else f"{since} 起"
    print(f"[{code}] 來源 {row_count} 列｜{scope}：回報 {len(reports)} 筆（含工種明細 {with_trades}）"
          f"、解析失敗 {len(rejects)} 則")
    if args.dry_run or (not reports and not rejects):
        return 0

    owned = _owned(reports)
    batches = [reports[i:i + CHUNK] for i in range(0, len(reports), CHUNK)] or [[]]
    done = 0
    for n, batch in enumerate(batches):
        last = n == len(batches) - 1
        body = {"reports": batch, "rejects": rejects if n == 0 else [],
                "owned": owned if last else []}
        try:
            r = requests.post(endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"},
                              timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            msg = f"推送失敗（{type(e).__name__}: {e}）"
            if isinstance(e, requests.HTTPError):
                msg += f"：{e.response.text[:200]}"
            return _fail(label, msg, bs.is_transient(e) and not args.all)
        done += r.json().get("reports", 0)
    print(f"[{code}] 已寫入出工資料庫：回報 {done} 筆（{len(batches)} 批）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="出工資料庫推送")
    ap.add_argument("--days", type=int, default=14, help="重送最近幾天（預設 14）")
    ap.add_argument("--all", action="store_true", help="推送來源內全部歷史（首次建庫用）")
    ap.add_argument("--dry-run", action="store_true", help="只解析並印出摘要，不推送")
    args = ap.parse_args()

    bs.utf8_stdio()
    bs.check_timezone()
    load_env()
    try:
        from collectors.worklog import worklog_sources
        sources = worklog_sources()
        if not sources:
            return _fail("", "未設定任何出工來源（WORKLOG_SHEET_URL／WORKLOG_URL_*）", False)

        endpoint = bs.cloud_endpoint("worklog")
        token = (os.environ.get("CLOUD_INGEST_TOKEN") or "").strip()
        if not args.dry_run and (not endpoint or not token):
            print("需設定 CLOUD_INGEST_URL 與 CLOUD_INGEST_TOKEN", file=sys.stderr)
            return 1

        since = None if args.all else date.today() - timedelta(days=args.days)
        # 逐工地處理，一個壞掉不影響其他工地；任一個「需要處理」整體就以非 0 結束
        failed = sum(_push_site(code, url, args, since, endpoint, token)
                     for code, url in sources.items())
    finally:
        bs.remove_tmp_db()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
