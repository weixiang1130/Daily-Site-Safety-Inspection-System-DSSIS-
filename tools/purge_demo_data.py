# -*- coding: utf-8 -*-
"""清除示範／測試資料，只保留由 tools/import_paper_records.py 補登的實際單據。

判斷依據：凡是掛在已停用工地（示範工地）上的單據與缺失一律刪除。
真實工地上的資料不會被動到。

用法：
    BASE=https://<站台網址> ADMIN_PASS=<密碼> python tools/purge_demo_data.py
    --dry-run  只列出將刪除的項目，不實際刪除
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8010").rstrip("/")
ACCOUNT = os.environ.get("ADMIN_USER", "admin")
PASSWORD = os.environ.get("ADMIN_PASS", "admin1234")
DRY = "--dry-run" in sys.argv

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(path, data=None, form=None):
    if form is not None:
        body, hdr = urllib.parse.urlencode(form).encode(), {}
    elif data is not None:
        body, hdr = json.dumps(data).encode(), {"Content-Type": "application/json"}
    else:
        body, hdr = None, {}
    try:
        with op.open(urllib.request.Request(BASE + path, body, hdr), timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]


def main():
    print(f"目標站台：{BASE}{'（試跑，不會刪除）' if DRY else ''}")
    s, r = call("/api/login", form={"username": ACCOUNT, "password": PASSWORD})
    if s != 200:
        raise SystemExit(f"登入失敗（HTTP {s}）：{r}")
    if r["user"]["role"] != "admin":
        raise SystemExit("需以管理員帳號執行")

    _, all_sites = call("/api/admin/sites")
    dead = {x["id"]: x["name"] for x in all_sites if not x["active"]}
    if not dead:
        print("沒有已停用的工地，無資料需要清除。")
        return
    print(f"已停用工地：{'、'.join(dead.values())}\n")

    _, coords = call("/api/coordinations?days=3650")
    _, insps = call("/api/inspections?days=3650")
    _, findings = call("/api/findings?days=3650")

    victims_c = [c for c in coords if c.get("site_id") in dead
                 or c.get("site") in dead.values()]
    victims_i = [i for i in insps if i.get("site_id") in dead]
    victims_f = [f for f in findings if f.get("site_id") in dead]

    print(f"將刪除：協議單 {len(victims_c)}、巡檢單 {len(victims_i)}、"
          f"缺失 {len(victims_f)}")
    for c in victims_c:
        print(f"    協議單 COORD-{c['id']:06d}　{c['work_date']}　{c['site']}")
    for i in victims_i:
        print(f"    巡檢單 INSP-{i['id']:06d}　{i['inspect_date']}　{i['form_title']}")

    if DRY:
        print("\n（試跑，未實際刪除）")
        return

    n = 0
    for f in victims_f:
        call(f"/api/admin/findings/{f['id']}", data={})
        n += 1
    for c in victims_c:
        call(f"/api/admin/coordinations/{c['id']}", data={})
    for i in victims_i:
        call(f"/api/admin/inspections/{i['id']}", data={})

    print(f"\n已刪除 {len(victims_c)} 張協議單、{len(victims_i)} 張巡檢單、{n} 筆缺失。")

    _, d = call("/api/dashboard?days=3650")
    k = d["kpi"]
    print(f"儀表板：缺失 {k['findings_range']}　未結案 {k['open']}　"
          f"逾期 {k['overdue']}　結案率 {k['closed_rate']}%")


if __name__ == "__main__":
    main()
