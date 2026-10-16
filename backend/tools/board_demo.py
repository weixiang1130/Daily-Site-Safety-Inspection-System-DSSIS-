"""工地看板本機模擬：用示範資料開看板，看版面與文字，不碰任何資料庫。

用途是改看板版面時先在本機看效果（含大螢幕全螢幕），不必推上雲端、
也不必動正在運作的地端伺服器。資料全是虛構的「示範工地 A」，但熱危害、
噪音與作業危害的措施文字直接呼叫後端實際程式產生（heat_guidance、
noise_guidance、work_hazards），牆上會看到的法規文字與正式環境一致。

    python backend/tools/board_demo.py                 # http://localhost:8130
    python backend/tools/board_demo.py --scenario hot  # 熱危害第三級＋噪音超標
    python backend/tools/board_demo.py --port 8131

只用標準函式庫；只提供看板會用到的三個 API 與 frontend/ 靜態檔。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(ROOT, "frontend")
sys.path.insert(0, os.path.join(ROOT, "backend", "onprem"))

from app.hazard import level_of                      # noqa: E402
from app.heat_guidance import heat_guidance          # noqa: E402
from app.noise_guidance import noise_guidance        # noqa: E402
from app.work_hazards import guide_of, hazards_of    # noqa: E402

SCENARIOS = {
    # 常見的夏天上午：熱危害第二級、噪音接近門檻
    "normal": {"heat_index": 35.4, "temperature": 31.2, "humidity": 70.2, "wind_speed": 1.8,
               "noise": 82.0, "pm25": 18.0, "pm10": 42.0},
    # 熱危害第三級＋噪音超過 8 小時容許值：看公告插播與擠版時的收合
    "hot": {"heat_index": 42.3, "temperature": 35.6, "humidity": 66.0, "wind_speed": 0.6,
            "noise": 93.4, "pm25": 37.6, "pm10": 92.1},
    # 涼爽安靜：熱危害未達第一級、噪音未達注意值
    "quiet": {"heat_index": 24.1, "temperature": 23.5, "humidity": 58.0, "wind_speed": 3.2,
              "noise": 71.0, "pm25": 9.0, "pm10": 20.0},
}

SCHEDULE = [
    ("08:00", "08:05", "安全早會"), ("08:05", "08:15", "工具箱會議（危害預知）"),
    ("08:15", "08:20", "作業前檢點"), ("08:20", "10:00", "安全作業"),
    ("10:00", "10:10", "上午休息"), ("10:10", "12:00", "安全作業"),
    ("12:00", "13:00", "午休"), ("13:00", "13:10", "下午作業前檢點"),
    ("13:10", "15:00", "安全作業"), ("15:00", "15:10", "下午休息"),
    ("15:10", "16:50", "安全作業"), ("16:50", "17:00", "收工整理"),
]

# (棟別, 廠商, 工種, 人數, 作業主管, 施作項目)——全為虛構
WORKLOG = [
    ("辦公棟", "甲工程行", "鋼筋", 12, "王主任", "4F柱筋綁紮、2F版筋"),
    ("辦公棟", "乙營造", "模板", 18, "李領班", "3F模板組立"),
    ("辦公棟", "丙水電", "水電", 6, "陳技師", "2F管路預埋"),
    ("辦公棟", "丁機電", "空調", 4, None, "風管吊架"),
    ("辦公棟", "戊工程", "泥作", 9, "林師傅", "1F砌磚"),
    ("辦公棟", "己公司", "鷹架", 5, None, "外牆施工架加設"),
    ("辦公棟", "庚實業", "防水", 3, None, "屋頂防水"),
    ("辦公棟", "丑鋼構", "鋼構", 8, "周工地", "第三節鋼構吊裝"),
    ("住宅棟", "辛工程", "鋼筋", 14, "張主任", "5F版筋"),
    ("住宅棟", "壬營造", "模板", 20, "吳領班", "6F模板"),
    ("住宅棟", "癸水電", "水電", 7, None, "4F配管"),
    ("住宅棟", "子塗裝", "油漆", 4, None, "梯間批土"),
    ("住宅棟", "寅混凝土", "混凝土", 6, None, "B1 底板 RC 澆置"),
]


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def board_data(scenario: str) -> dict:
    m = dict(SCENARIOS[scenario])
    levels = {k: level_of(k, v) for k, v in m.items() if k in ("pm25", "pm10", "noise", "heat_index")}
    # 每分鐘小幅變動（看板每分鐘輪詢一次），才看得到數字滾動與換頁效果
    tick = datetime.now().minute
    rows = [{"building": b, "vendor": v, "trade": t, "headcount": n + (tick % 3 if i == 0 else 0),
             "supervisor": s, "tasks": task}
            for i, (b, v, t, n, s, task) in enumerate(WORKLOG)]
    # 與 app.main.hazards_section 相同的對應方式（不 import main：那會連資料庫）
    hz = {}
    for r in rows:
        for label in hazards_of(f"{r['trade']} {r['tasks']}"):
            lst = hz.setdefault(label, [])
            src = f"{r['vendor']}（{r['building']}）"
            if src not in lst and len(lst) < 6:
                lst.append(src)
    hazards = sorted([{"label": k, "sources": v, "measures": guide_of(k)} for k, v in hz.items()],
                     key=lambda x: -len(x["sources"]))
    return {
        "generated_at": _now_iso(),
        "stale_after_min": 180,
        "station": {"site_code": "DEMO", "reading_at": _now_iso(), "metrics": m, "levels": levels,
                    "heat": heat_guidance(m["heat_index"]), "noise": noise_guidance(m["noise"])},
        "stats": {"findings_today": 2 + tick % 2, "open": 5 + (tick // 2) % 2, "overdue": 1},
        "worklog": {"date": date.today().isoformat(), "total": sum(r["headcount"] for r in rows), "rows": rows},
        "record": {"days": (date.today() - date(2024, 9, 12)).days, "since": "2024-09-12",
                   "last_month_mandays": 5277},
        "hazards": hazards,
        # 門禁在場人數（人臉辨識）：在場人數隨分鐘小幅變動
        "headcount": {"headcount_present": 92.0 + tick % 4, "headcount_in": 150.0 + tick % 3,
                      "headcount_out": 58.0, "reading_at": _now_iso()[:16]},
        "news": [{"title": "（示範）職安署提醒高氣溫戶外作業熱危害預防",
                  "published": (date.today() - timedelta(days=1)).isoformat()}],
        "board": {"site_id": 1, "site_code": "DEMO", "site_name": "示範工地 A", "revision": 1, "config": {
            "schedule": [{"start": a, "end": b, "label": c} for a, b, c in SCHEDULE],
            "contacts": [
                {"role": "工地主任", "name": "王大明", "phone": "0912-000-111"},
                {"role": "職安人員", "name": "李小華", "phone": "0922-000-222"},
                {"role": "消防隊", "name": "119", "phone": "119"},
                {"role": "鄰近醫院急診", "name": "示範醫院", "phone": "02-2000-0000"},
                {"role": "警察局", "name": "110", "phone": "110"},
            ],
            "announcements": [{"title": "高處作業請確實使用安全帶",
                               "body": "2 公尺以上作業一律掛鉤，主管每日巡查。"}],
            "safety": {}, "updated_at": _now_iso()}},
    }


BRANDING = {"system_name": "職安填報系統", "war_room_name": "職安戰情室",
            "org_short": "示範營造", "primary_site_code": "DEMO",
            "buildings": ["辦公棟", "住宅棟"], "war_room": True, "wallboard": False}


class Handler(BaseHTTPRequestHandler):
    scenario = "normal"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                       # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/static/dashboard.html")
            self.end_headers()
            return
        if path == "/api/branding":
            return self._json(BRANDING)
        if path == "/api/board-sites":
            return self._json([{"id": 1, "code": "DEMO", "name": "示範工地 A"}])
        if path.startswith("/api/board-data/"):
            return self._json(board_data(self.scenario))
        if path in ("/api/brand/mark", "/api/brand/wordmark"):
            # 與地端相同：backend/data/brand-*.local.png，沒有就 404（看板顯示文字）
            full = os.path.join(ROOT, "backend", "data", f"brand-{path.rsplit('/', 1)[1]}.local.png")
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            full = os.path.realpath(os.path.join(FRONTEND, rel))
            if full.startswith(os.path.realpath(FRONTEND) + os.sep) and os.path.isfile(full):
                with open(full, "rb") as f:
                    body = f.read()
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")   # 改了檔重新整理就看得到
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self._json({"detail": "找不到路由"}, 404)

    def log_message(self, *args):                           # 安靜：只印啟動訊息
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8130)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal")
    args = ap.parse_args()
    Handler.scenario = args.scenario
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"工地看板本機模擬（情境 {args.scenario}）：http://localhost:{args.port}/static/dashboard.html")
    srv.serve_forever()


if __name__ == "__main__":
    main()
