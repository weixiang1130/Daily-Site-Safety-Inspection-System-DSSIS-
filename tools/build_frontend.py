# -*- coding: utf-8 -*-
"""建置 Netlify 前端產出。

把 static/ 複製到 dist/static/，並產生根目錄導頁。
API 由同一個 Netlify 站台的 Functions 提供（/api/*），因此不需要任何代理設定。

本機執行：
    python tools/build_frontend.py
"""
import os
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def main():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    shutil.copytree(SRC, os.path.join(DIST, "static"))
    n = sum(len(f) for _, _, f in os.walk(os.path.join(DIST, "static")))
    print(f"[build] 複製 frontend/ → dist/static/（{n} 個檔案）")

    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as f:
        f.write(ROOT_INDEX)

    with open(os.path.join(DIST, "_redirects"), "w", encoding="utf-8", newline="\n") as f:
        f.write(REDIRECTS)

    print("[build] 完成")


if __name__ == "__main__":
    main()
