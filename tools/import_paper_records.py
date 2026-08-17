# -*- coding: utf-8 -*-
"""把現行紙本單據匯入系統，讓儀表板有真實的內容基礎。

資料來源為三份現行單據（內容照實，識別身分的名稱依 deid_rules.local.json 代稱）：
  1. 每日協議、巡視及處理紀錄表（手寫掃描）
  2. 某工地每日巡檢（手寫掃描）
  3. 應變小組日報（電子檔，含缺失照片與責任廠商）

用法：
    BASE=https://<站台網址> python tools/import_paper_records.py
    # 預設連本機 http://127.0.0.1:8010

    --real  改用真實姓名、廠商名與工地名。
            真實名稱不寫在本檔，而是啟動時從 deid_rules.local.json 反查
            （該檔已列入 .gitignore）。因此本腳本可以安全進版控，
            而線上站台仍能載入真實資料。

            **使用前提：系統已改掉預設密碼。** repo 中的 migration 含
            預設密碼，未更換前任何人讀了 GitHub 就能登入看到資料。
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8010").rstrip("/")
ACCOUNT = os.environ.get("IMPORT_USER", "safe01")
PASSWORD = os.environ.get("IMPORT_PASS", "safe1234")
USE_REAL = "--real" in sys.argv

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# 1x1 透明 PNG。實際簽名應由現場於手機上手寫，這裡僅為補登既有紙本紀錄。
PLACEHOLDER_SIG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def call(path, data=None, form=None):
    if form is not None:
        body, hdr = urllib.parse.urlencode(form).encode(), {}
    elif data is not None:
        body, hdr = json.dumps(data).encode(), {"Content-Type": "application/json"}
    else:
        body, hdr = None, {}
    req = urllib.request.Request(BASE + path, body, hdr)
    try:
        with op.open(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


# ---------------------------------------------------------------------------
# 單據內容
# 缺失文字、災害分類、改善方式與責任歸屬結構均照原始單據。
# 廠商與人員以代號表示，實際名稱在載入時才對應。
# ---------------------------------------------------------------------------
V = {"A": 0, "B": 1, "C": 2, "D": 3}      # 廠商索引（對應 /api/vendors 順序）

# 人員代稱。--real 模式下會依 deid_rules.local.json 反查回真實姓名，
# 真實姓名不出現在本檔，因此本檔可安全進版控。
P = {"p1": "王小明", "p2": "陳小華", "p3": "林小美",
     "p4": "張小龍", "p5": "李小強", "p6": "黃小天",
     "p7": "吳小方", "p8": "鄭小雲"}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(BASE_DIR, "deid_rules.local.json")


def load_reverse_map():
    """代稱 → 真實名稱。找不到對照表時回傳空 dict（維持代稱）。"""
    if not os.path.exists(RULES):
        raise SystemExit(
            "--real 需要 deid_rules.local.json，但找不到該檔。\n"
            "該檔為真實名稱對照表，不進版控，請於本機建立後再執行。")
    with open(RULES, encoding="utf-8") as f:
        terms = json.load(f).get("terms", [])
    # 同一個代稱可能對應多個真實字詞（例如不同寫法），取第一個
    rev = {}
    for t in terms:
        rev.setdefault(t["public"], t["real"])
    return rev


def realname(name, rev):
    return rev.get(name, name) if rev else name

# ---- 單據三：應變小組日報 ----
DAILY_REPORT = {
    "site": 2,                    # 示範工地 C
    "work_date": "2026-08-03",
    "agreement": (
        "本日現場持續進行各項收尾及設備安裝工程，施工內容包含噴漆、高架地板安裝、"
        "泥作粉刷、貼磚、輕隔間批土、鐵件預埋、鋁板填縫及封板、結晶化玻璃安裝、"
        "防火捲門及防火門工程、景觀模板組立、水溝工程收尾及路沿石修補、"
        "伸縮縫補強、施工電梯及堆高機操作等作業。\n"
        "針對高架作業、機具操作及設備安裝等高風險工項，加強施工前巡檢作業區域安全管制，"
        "要求人員確實穿戴個人防護具並落實安全防護；持續執行各項施工範圍環境整理整頓，"
        "確認現場防護設施完善、施工動線順暢。"),
    "handling": "本日缺失均已於現場派員立即改善完成，並於應變小組會議檢討。",
    "attendees": [
        {"work_item": "應變小組", "vendor": "D", "trade": "移工", "person": "p1"},
        {"work_item": "應變小組", "vendor": "D", "trade": "點工", "person": "p2"},
    ],
    "findings": [
        ("施工架踏板倚靠欄杆未移除", "FALL", "墜落", "A", "p1", "派員立即移除"),
        ("木材鋸台鋸齒未加裝安全防護", "CAUGHT", "被夾被捲", "B", "p2",
         "派員立即將鋸齒部分覆蓋改善"),
        ("施工架及上下設備未有安全防護", "FALL", "墜落", "C", "p3", "派員立即改善"),
        ("電銲作業未設置滅火器", "FIRE", "火災", "C", "p3", "派員立即設置滅火器"),
        ("預留鋼筋斷面未有安全防護", "PUNCTURE", "穿刺", "D", "p4",
         "派員立即加裝安全防護"),
        ("不合格施工架", "COLLAPSE", "倒塌崩塌", "D", "p5", "派員立即移除"),
        ("施工架踏板倚靠欄杆未移除", "FALL", "墜落", "A", "p1", "派員立即移除"),
        ("施工架上下設備拆除未復原", "FALL", "墜落", "A", "p1", "派員立即復原"),
        ("木材鋸台鋸齒未加裝安全防護", "CAUGHT", "被夾被捲", "B", "p2",
         "派員立即將鋸齒部分覆蓋改善"),
    ],
}

# ---- 單據一：每日協議、巡視及處理紀錄表 ----
COORD_A = {
    "site": 0,                    # 示範工地 A
    "work_date": "2026-08-03",
    "agreement": (
        "1. 人員高處作業須穿著全身式安全帶，並將安全帶掛勾扣掛於堅固錨錠或安全母索。\n"
        "2. 天氣炎熱，人員須注意個人身體熱負荷狀況，工區提供飲用水，"
        "避免人員中暑事件發生；若身體不適須立即停止作業並回報管理職。"),
    "handling": "巡視所列 1～6 項均已當場要求改善並完成。",
    "attendees": [
        {"work_item": "泥作", "vendor": "A", "trade": "泥作", "person": "p7"},
        {"work_item": "機電", "vendor": "B", "trade": "機電", "person": "p8"},
        {"work_item": "施工架", "vendor": "C", "trade": "施工架", "person": "p3"},
    ],
    "findings": [
        ("施工架踏板未確實鋪設", "FALL", "墜落", "C", "p3", "當場改善完成"),
        ("人員未確實佩掛安全防護", "PPE", "門禁與防護具", "A", "p7", "當場改善完成"),
        ("設備未有安全防護", "MACHINE", "危險機械吊掛", "B", "p8", "當場改善完成"),
        ("電銲作業未設置滅火器", "FIRE", "火災", "B", "p8", "當場改善完成"),
        ("鋼筋斷面未有安全防護", "PUNCTURE", "穿刺", "C", "p3", "當場改善完成"),
        ("施工架上下設備拆除未復原", "FALL", "墜落", "C", "p3", "當場改善完成"),
    ],
}

# ---- 單據二：每日巡檢 ----
COORD_B = {
    "site": 1,                    # 示範工地 B
    "work_date": "2026-08-05",
    "agreement": (
        "本日各承攬商作業範圍及風險已於協議會議中告知，"
        "地下室通高處及高處車輛作業區域列為本日重點管制。"),
    "handling": (
        "1. 地下室通高位置墜落風險，設置護欄。\n"
        "2. 檢查各樓層高處車輛及護欄設置狀況。"),
    "attendees": [
        {"work_item": "鋼筋模板", "vendor": "A", "trade": "鋼筋模板",
         "person": "p7", "content": "A棟 136-136 BH"},
        {"work_item": "機電", "vendor": "B", "trade": "機電",
         "person": "p8", "content": "全區機電管線"},
        {"work_item": "排水工程", "vendor": "C", "trade": "排水",
         "person": "p3", "content": "軌道排水／U 溝 104-43／57"},
        {"work_item": "鋼構組立", "vendor": "D", "trade": "鋼構",
         "person": "p4", "content": "鋼構組立／道路作業"},
    ],
    "findings": [
        # 原單「改善完成日期」註明「護欄當日設置完成」，因此雖為限期改善但已結案
        ("地下室通高位置墜落風險，護欄未依規定設置", "FALL", "墜落", "D", "p6",
         "護欄當日設置完成", "2026-08-05"),
        ("焊機、延長線未依規定配置", "ELEC", "感電", "B", "p8", "當場改善完成"),
    ],
}


def build(doc, sites, vendors, rev):
    findings = []
    for f in doc["findings"]:
        desc, hz, hzl, vend, person = f[0], f[1], f[2], f[3], f[4]
        scheduled = len(f) > 6
        item = {
            "description": desc, "hazard_code": hz, "hazard_label": hzl,
            "vendor_id": vendors[V[vend]]["id"],
            "responsible_person": realname(P[person], rev),
            "severity": "major" if hz in ("FALL", "COLLAPSE") else "minor",
        }
        if scheduled:
            item.update(action_type="scheduled", due_date=f[6], fix_note=f[5])
        else:
            item.update(action_type="onsite", fix_note=f[5])
        findings.append(item)

    return {
        "site_id": sites[doc["site"]]["id"],
        "meeting_date": doc["work_date"],
        "work_date": doc["work_date"],
        "weather": "晴",
        "agreement_text": doc["agreement"],
        "handling_text": doc["handling"],
        "attendees": [{
            "work_item": a["work_item"],
            "vendor_id": vendors[V[a["vendor"]]]["id"],
            "vendor_name": vendors[V[a["vendor"]]]["name"],
            "trade": a["trade"], "person_name": realname(P[a["person"]], rev),
            "work_content": a.get("content", a["work_item"]),
        } for a in doc["attendees"]],
        "findings": findings,
        "signatures": [{"role": "職安人員", "signer_name": "（補登紙本紀錄）",
                        "image": PLACEHOLDER_SIG}],
    }


def closeout(finding_ids, findings):
    """補登既有紙本紀錄：走完改善與複驗，讓狀態與紙本一致。"""
    n = 0
    for fid, f in zip(finding_ids, findings):
        if not f.get("fix_note"):
            continue
        if f.get("action_type") == "scheduled":
            call(f"/api/findings/{fid}/fix", data={"fix_note": f["fix_note"]})
        s, _ = call(f"/api/findings/{fid}/verify", data={})
        if s == 200:
            n += 1
    return n


def main():
    rev = {}
    if USE_REAL:
        rev = load_reverse_map()
        print(f"模式：真實資料（載入 {len(rev)} 條對照）")
        print("      本模式會寫入真實姓名、廠商名與工地名到目標站台。")
        print("      請確認該站台已改掉預設密碼且存取受控。\n")
    else:
        print("模式：代稱資料（加 --real 可改用真實名稱）\n")

    print(f"目標站台：{BASE}")
    s, r = call("/api/login", form={"username": ACCOUNT, "password": PASSWORD})
    if s != 200:
        raise SystemExit(f"登入失敗（HTTP {s}）：{r}")
    print(f"登入：{r['user']['name']}（{r['user']['role']}）\n")

    _, sites = call("/api/sites")
    _, vendors = call("/api/vendors")
    if len(sites) < 3 or len(vendors) < 4:
        raise SystemExit("主檔不足，請先確認 migration 已套用")

    total_findings = 0
    for name, doc in [("應變小組日報", DAILY_REPORT),
                      ("每日協議巡視處理紀錄表", COORD_A),
                      ("每日巡檢", COORD_B)]:
        payload = build(doc, sites, vendors, rev)
        s, r = call("/api/coordinations", data=payload)
        if s != 200:
            print(f"✗ {name}　匯入失敗（HTTP {s}）：{r}")
            continue
        n = len(payload["findings"])
        total_findings += n

        # 原始單據上這些缺失都已註明改善完成並經職安簽核，
        # 因此補登時一併走完「改善 → 複驗結案」，否則儀表板會顯示
        # 結案率 0%、且已完成的限期改善項目被誤判為逾期。
        closed = closeout(r.get("finding_ids", []), payload["findings"])

        print(f"✓ {name}　{doc['work_date']}　"
              f"{sites[doc['site']]['name']}　"
              f"COORD-{r['coordination_id']:06d}　缺失 {n} 筆（結案 {closed}）")

    s, d = call("/api/dashboard?days=30")
    if s == 200:
        k = d["kpi"]
        print(f"\n匯入完成，共 {total_findings} 筆缺失。")
        print(f"儀表板：缺失 {k['findings_range']} 筆／未結案 {k['open']}／"
              f"逾期 {k['overdue']}／結案率 {k['closed_rate']}%")
        print("災害分布：" + "、".join(
            f"{x['label']} {x['count']}" for x in d["by_hazard"][:6]))
        print("廠商排行：" + "、".join(
            f"{x['label']} {x['count']}" for x in d["by_vendor"][:5]))


if __name__ == "__main__":
    main()
