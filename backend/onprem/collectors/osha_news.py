"""抓取職安署網站的新聞稿清單，供工地看板「安全佈告／宣導」輪播職安新知。

來源是政府公開網站的新聞稿列表頁（伺服器渲染的 HTML，無官方 API），
以正規表示式解析清單項目。網站改版時解析會抓到 0 筆——這種情況只記錄
不報錯，看板少一類內容但其他輪播照常。

設定（見 .env.onprem.example）：

    OSHA_NEWS_INTERVAL   每輪間隔秒數，預設 21600（6 小時；新聞稿一天
                         沒幾則，抓太密只是打擾人家網站）

用法（在 backend/onprem 目錄下執行）：

    python -m collectors.osha_news            # 跑一輪就結束
    python -m collectors.osha_news --loop     # 持續執行
"""

from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件，請先執行：pip install requests")

from app.db import NewsItem, SessionLocal, init_db

from .config import env, load_env, log

TIMEOUT = 30
DEFAULT_INTERVAL = 21600
KEEP = 30           # 只留最新 N 則，看板輪播用不到更舊的

# 職安署「新聞稿」列表頁。政府公開網址，非公司資料。
LIST_URL = "https://www.osha.gov.tw/48110/48417/48419/lpsimplelist"
BASE_URL = "https://www.osha.gov.tw"

# 清單項目長這樣（見列表頁原始碼的 item_list2 區塊）：
#   <a href="/48110/48417/48419/211262/post" title="標題">…</a>
#   …<span>發布日期：2026-08-11</span>
ITEM_RE = re.compile(
    r'<a href="(/48110/48417/48419/(\d+)/post)"[^>]*title="([^"]+)"', re.S)
DATE_RE = re.compile(r"發布日期[：:](\d{4}-\d{2}-\d{2})")


def parse_list(html: str) -> list:
    """列表頁 HTML → [{news_id, title, url, published}]，依出現順序。"""
    items = []
    for m in ITEM_RE.finditer(html):
        path, news_id, title = m.groups()
        # 發布日期在同一個項目區塊內、連結之後；往後找最近的一個
        dm = DATE_RE.search(html, m.end(), m.end() + 800)
        published = None
        if dm:
            try:
                published = date.fromisoformat(dm.group(1))
            except ValueError:
                pass
        items.append({"news_id": news_id, "title": title.strip()[:255],
                      "url": BASE_URL + path, "published": published})
    return items


def poll_once() -> str:
    r = requests.get(LIST_URL, timeout=TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0 (site-safety-board)"})
    r.raise_for_status()
    items = parse_list(r.text)
    if not items:
        return "解析到 0 則——網站可能改版，請檢查 ITEM_RE 是否仍符合頁面結構"

    db = SessionLocal()
    created = 0
    try:
        for it in items:
            obj = (db.query(NewsItem)
                   .filter(NewsItem.source == "osha",
                           NewsItem.news_id == it["news_id"]).first())
            if not obj:
                obj = NewsItem(source="osha", news_id=it["news_id"])
                db.add(obj)
                created += 1
            obj.title = it["title"]
            obj.url = it["url"]
            obj.published = it["published"]
            obj.fetched_at = datetime.now()
        db.flush()
        # 修剪舊聞：看板只輪播最新幾則，沒必要無限累積
        # DESC 排序時 SQLite 與 SQL Server 都把 NULL 排最後，
        # 不用 NULLS LAST——SQL Server 根本不支援那個語法
        keep_ids = [n.id for n in db.query(NewsItem)
                    .filter(NewsItem.source == "osha")
                    .order_by(NewsItem.published.desc(),
                              NewsItem.id.desc()).limit(KEEP).all()]
        if keep_ids:
            (db.query(NewsItem)
             .filter(NewsItem.source == "osha", ~NewsItem.id.in_(keep_ids))
             .delete(synchronize_session=False))
        db.commit()
    finally:
        db.close()
    return f"取得 {len(items)} 則，新增 {created}"


def main() -> None:
    load_env()
    init_db()
    interval = int(env("OSHA_NEWS_INTERVAL", str(DEFAULT_INTERVAL)) or DEFAULT_INTERVAL)
    loop = "--loop" in sys.argv
    while True:
        try:
            log(poll_once())
        except Exception as e:                          # noqa: BLE001
            log(f"本輪失敗：{e}")
        if not loop:
            return
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已停止")
