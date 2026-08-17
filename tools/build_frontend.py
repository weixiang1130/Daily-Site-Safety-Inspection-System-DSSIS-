# -*- coding: utf-8 -*-
"""建置 Netlify 前端產出。

把 static/ 複製到 dist/static/，並依環境變數 BACKEND_ORIGIN 產生 _redirects，
將 /api/* 與 /uploads/* 反向代理到 Python 後端。

使用者從頭到尾只看到一個 Netlify 網址；瀏覽器認為 API 與前端同源，
因此 cookie 正常運作、不需要處理 CORS。

環境變數：
    BACKEND_ORIGIN   後端網址，例如 https://safety-ops-api.onrender.com
                     （在 Netlify 後台 Site settings → Environment variables 設定）

本機測試：
    BACKEND_ORIGIN=http://127.0.0.1:8010 python tools/build_frontend.py
"""
import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "static")
DIST = os.path.join(BASE_DIR, "dist")

BACKEND = os.environ.get("BACKEND_ORIGIN", "").rstrip("/")

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


def main():
    if not BACKEND:
        print("[build] 警告：未設定 BACKEND_ORIGIN，API 代理不會生效。", file=sys.stderr)
        print("[build]        請在 Netlify 環境變數中設定後端網址後重新部署。",
              file=sys.stderr)

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    shutil.copytree(SRC, os.path.join(DIST, "static"))
    n = sum(len(f) for _, _, f in os.walk(os.path.join(DIST, "static")))
    print(f"[build] 複製 static/ → dist/static/（{n} 個檔案）")

    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as f:
        f.write(ROOT_INDEX)

    lines = []
    if BACKEND:
        lines += [
            f"/api/*      {BACKEND}/api/:splat      200",
            f"/uploads/*  {BACKEND}/uploads/:splat  200",
        ]
    lines.append("/  /static/index.html  302")

    with open(os.path.join(DIST, "_redirects"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[build] 產生 _redirects，後端指向：{BACKEND or '（未設定）'}")
    print("[build] 完成")


if __name__ == "__main__":
    main()
