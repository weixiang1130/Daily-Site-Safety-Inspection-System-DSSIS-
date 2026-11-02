# -*- coding: utf-8 -*-
"""建置 Netlify 前端產出。

把 static/ 複製到 dist/static/，並產生根目錄導頁。
API 由同一個 Netlify 站台的 Functions 提供（/api/*），因此不需要任何代理設定。

本機執行：
    python backend/tools/build_frontend.py
"""
import hashlib
import os
import re
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(BASE_DIR, "frontend")
DIST = os.path.join(BASE_DIR, "dist")

# 前端發布在 /static/ 底下（而非站台根目錄），因此原始碼資料夾改名為
# frontend/ 之後，網址仍維持 /static/*，HTML 內的路徑不需要更動。

ROOT_INDEX = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=/static/index.html">
<title>載入中</title>
</head>
<body><p>載入中… <a href="/static/index.html">若未自動跳轉請點此</a></p></body>
</html>
"""

# /api/* 由 Functions 接手（函式自身的 config.path 已宣告），
# 這裡只處理根路徑導頁與 SPA 式的靜態回退。
REDIRECTS = "/  /static/index.html  302\n"

# 不上雲端的頁面。
#
# 戰情室主體在公司內網執行（見 docs/地端戰情室.md）。雲端仍部署全部頁面：
#   * dashboard-detail.html（原戰情室）帶 ?k=<看板權杖> 走「看板模式」，
#     只讀地端推上來的靜態快照（Blob），不查資料庫、不接監視器
#   * dashboard.html（工地看板）的資料端點只在地端，在雲端開啟時
#     會自動轉往 dashboard-detail（見 site-board.js 開頭），不會留下
#     一個每分鐘空打 404 的輪詢
CLOUD_EXCLUDE = ()

# HTML 參照的 CSS／JS 網址帶內容雜湊，這些檔案就能長期快取：內容沒變不用回伺服器
# 確認，改了網址也跟著變。HTML 本身每次都確認，部署後才拿得到新網址。
ASSET_REF = re.compile(r'(/static/[\w.-]+\.(?:css|js))(?=["\'])')
LONG_CACHE = "  Cache-Control: public, max-age=31536000, immutable\n"


def version_assets(static_dir):
    """改寫 HTML 的 CSS／JS 網址，回傳要長期快取的路徑。"""
    used = set()

    def stamp(m):
        path = os.path.join(static_dir, os.path.basename(m.group(1)))
        if not os.path.isfile(path):
            return m.group(1)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:10]
        used.add(m.group(1))
        return f"{m.group(1)}?v={digest}"

    for name in os.listdir(static_dir):
        if not name.endswith(".html"):
            continue
        path = os.path.join(static_dir, name)
        with open(path, encoding="utf-8", newline="") as f:
            html = f.read()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(ASSET_REF.sub(stamp, html))
    return sorted(used)


def main():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    shutil.copytree(SRC, os.path.join(DIST, "static"),
                    ignore=shutil.ignore_patterns(*CLOUD_EXCLUDE))
    n = sum(len(f) for _, _, f in os.walk(os.path.join(DIST, "static")))
    print(f"[build] 複製 frontend/ → dist/static/（{n} 個檔案）")

    if CLOUD_EXCLUDE:
        print("[build] not deployed to cloud: " + ", ".join(CLOUD_EXCLUDE))

    assets = version_assets(os.path.join(DIST, "static"))
    with open(os.path.join(DIST, "_headers"), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(f"{a}\n{LONG_CACHE}" for a in assets))
    print(f"[build] 長期快取 {len(assets)} 個 CSS／JS")

    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as f:
        f.write(ROOT_INDEX)

    with open(os.path.join(DIST, "_redirects"), "w", encoding="utf-8", newline="\n") as f:
        f.write(REDIRECTS)

    print("[build] 完成")


if __name__ == "__main__":
    main()
