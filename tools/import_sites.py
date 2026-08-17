# -*- coding: utf-8 -*-
"""依工地體系表匯入工地主檔。

工地名稱屬公司資料，放在 data/sites.local.json（已列入 .gitignore），
由本工具在執行時透過 API 送進站台，不隨程式碼進版控。

用法：
    BASE=https://<站台網址> python tools/import_sites.py
    BASE=... python tools/import_sites.py --deactivate-others
        另把不在清單內的既有工地停用（示範工地等）。
        停用不會刪除既有紀錄，只是不再出現在填報選單。

需以管理員帳號執行（預設 admin）。
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
DEACTIVATE = "--deactivate-others" in sys.argv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE_DIR, "data", "sites.local.json")

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
        return e.code, e.read().decode("utf-8", "replace")[:300]


def main():
    if not os.path.exists(DATA):
        raise SystemExit(
            f"找不到 {DATA}。\n"
            "該檔含公司真實工地名稱，不進版控，請於本機依體系表建立。")

    with open(DATA, encoding="utf-8") as f:
        depts = json.load(f)["departments"]

    print(f"目標站台：{BASE}")
    s, r = call("/api/login", form={"username": ACCOUNT, "password": PASSWORD})
    if s != 200:
        raise SystemExit(f"登入失敗（HTTP {s}）：{r}")
    if r["user"]["role"] != "admin":
        raise SystemExit("需以管理員帳號執行")
    print(f"登入：{r['user']['name']}\n")

    wanted, created = set(), 0
    for d in depts:
        print(f"■ {d['name']}")
        for i, name in enumerate(d["sites"], 1):
            code = f"{d['prefix']}{i:02d}"
            wanted.add(code)
            s, _ = call("/api/admin/sites", data={
                "code": code, "name": name,
                "department": d["name"], "sort_order": i,
            })
            if s == 200:
                created += 1
                print(f"    {code}  {name}")
            else:
                print(f"    ✗ {code}  {name}　失敗")
        print()

    if DEACTIVATE:
        s, existing = call("/api/admin/sites")
        n = 0
        for site in existing:
            if site["code"] not in wanted and site["active"]:
                call(f"/api/admin/sites/{site['id']}", data={"active": False})
                print(f"停用：{site['code']}  {site['name']}")
                n += 1
        if n:
            print(f"共停用 {n} 個非清單內工地（既有紀錄保留）\n")

    s, sites = call("/api/sites")
    by_dept = {}
    for site in sites:
        by_dept.setdefault(site.get("department") or "（未分處）", []).append(site)
    print(f"匯入完成，共 {created} 個工地。目前啟用中：")
    for dept, rows in by_dept.items():
        print(f"    {dept}　{len(rows)} 個")


if __name__ == "__main__":
    main()
