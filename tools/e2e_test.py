# -*- coding: utf-8 -*-
"""端到端煙霧測試。

需先啟動服務：uvicorn app.main:app --port 8010
執行：       python tools/e2e_test.py

依序驗證：登入 → 取表單 → 送出巡檢單（含缺失＋簽名）→ 取回 PDF
          → 送出協議巡視表 → 設備廠商推送 → 儀表板彙總
"""
import json
import http.cookiejar
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8010"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# 1x1 透明 PNG，當作測試用簽名圖
SIG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
       "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def post_form(path, data):
    req = urllib.request.Request(BASE + path, urllib.parse.urlencode(data).encode())
    return json.load(op.open(req))


def post_json(path, obj):
    req = urllib.request.Request(BASE + path, json.dumps(obj).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(op.open(req))


def get(path):
    return json.load(op.open(BASE + path))


def main():
    ok = True

    h = get("/api/health")
    print(f"0) 健康檢查　資料庫={h['database']}　"
          f"模板={h['form_templates']} 張／{h['form_items']} 項")

    me = post_form("/api/login", {"username": "insp03", "password": "insp1234"})
    print("1) 登入　", me["user"]["name"], me["user"]["role"])

    sites = get("/api/sites")
    print("2) 工地　", [s["name"] for s in sites])

    form = get("/api/forms/F19")
    items = form["items"]
    print("3) 表單　", form["title"], len(items), "項")

    results = [{"item_id": it["id"], "result": "pass"} for it in items]
    results[3].update(result="fail", remark="已請廠商當場改善")
    results[7].update(result="fail", remark="限期改善")
    results[10]["result"] = "na"

    findings = [
        {"item_id": items[3]["id"], "description": "施工架未設置踏腳板",
         "hazard_code": items[3]["hazard_code"],
         "hazard_label": items[3]["hazard_label"],
         "vendor_id": 1, "responsible_person": "王小明", "severity": "major",
         "action_type": "onsite", "fix_note": "派員立即補設"},
        {"item_id": items[7]["id"], "description": "施工架繫壁桿數量不足",
         "hazard_code": items[7]["hazard_code"],
         "hazard_label": items[7]["hazard_label"],
         "vendor_id": 2, "responsible_person": "陳小華", "severity": "critical",
         "action_type": "scheduled", "due_date": "2026-08-20"},
    ]

    r = post_json("/api/inspections", {
        "site_id": sites[1]["id"], "form_code": "F19",
        "location": "地下室 B2 東側", "weather": "晴",
        "results": results, "findings": findings,
        "signatures": [{"role": "檢查人員", "signer_name": me["user"]["name"],
                        "image": SIG}],
    })
    print(f"4) 巡檢單　INSP-{r['inspection_id']:06d}，開立 {len(r['finding_ids'])} 筆缺失")

    pdf = op.open(BASE + r["pdf_url"]).read()
    ok &= pdf[:5] == b"%PDF-"
    print(f"5) 巡檢 PDF　{len(pdf)} bytes　{'OK' if pdf[:5] == b'%PDF-' else '失敗'}")

    c = post_json("/api/coordinations", {
        "site_id": sites[1]["id"], "weather": "晴",
        "agreement_text": "1. 高架作業人員須佩戴全身式安全帶。\n2. 天氣炎熱注意熱危害。",
        "handling_text": "1. 地下室通高位置設置護欄。",
        "attendees": [
            {"work_item": "鋼筋模板", "vendor_id": 1, "vendor_name": "甲營造",
             "trade": "鋼筋", "person_name": "吳小方", "work_content": "A區鋼筋綁紮"},
            {"work_item": "機電", "vendor_id": 2, "vendor_name": "乙機電",
             "trade": "機電", "person_name": "鄭小雲", "work_content": "管線配設"},
        ],
        "findings": [{"description": "地下室通高位置未設置護欄",
                      "hazard_code": "FALL", "hazard_label": "墜落",
                      "location": "B2", "severity": "major", "vendor_id": 4,
                      "responsible_person": "黃小天",
                      "action_type": "scheduled", "due_date": "2026-08-19"}],
        "signatures": [{"role": "工程專案主管", "signer_name": me["user"]["name"],
                        "image": SIG}],
    })
    pdf2 = op.open(BASE + c["pdf_url"]).read()
    ok &= pdf2[:5] == b"%PDF-"
    print(f"6) 協議表　COORD-{c['coordination_id']:06d}，"
          f"PDF {len(pdf2)} bytes　{'OK' if pdf2[:5] == b'%PDF-' else '失敗'}")

    req = urllib.request.Request(
        BASE + "/api/v1/ingest/device",
        json.dumps({
            "vendor_code": "vendor-a", "site_code": "SITE-A",
            "device_type": "access", "device_id": "GATE-01",
            "readings": [
                {"metric": "headcount_in", "value_num": 128,
                 "reading_at": "2026-08-17T09:00:00"},
                {"metric": "headcount_out", "value_num": 12,
                 "reading_at": "2026-08-17T09:00:00"},
                {"metric": "alarm", "value_text": "未戴安全帽",
                 "reading_at": "2026-08-17T09:03:12"},
            ],
        }).encode(),
        {"Content-Type": "application/json",
         "X-Vendor-Token": "demo-token-vendor-a"})
    ing = json.load(urllib.request.urlopen(req))
    ok &= ing.get("accepted") == 3
    print(f"7) 設備推送　接收 {ing.get('accepted')} 筆")

    latest = get("/api/v1/device/latest?site_code=SITE-A&limit=3")
    print(f"8) 設備查詢　{len(latest)} 筆，最新 metric={latest[0]['metric']}"
          if latest else "8) 設備查詢　無資料")

    d = get("/api/dashboard?days=30")
    k = d["kpi"]
    print(f"9) 儀表板　今日缺失 {k['findings_today']}／未結案 {k['open']}／"
          f"逾期 {k['overdue']}／結案率 {k['closed_rate']}%")
    print(f"          災害分布 {[(x['label'], x['count']) for x in d['by_hazard'][:3]]}")
    print(f"          廠商排行 {[(x['label'], x['count']) for x in d['by_vendor'][:3]]}")

    print("\n" + ("全部通過" if ok else "有項目失敗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
