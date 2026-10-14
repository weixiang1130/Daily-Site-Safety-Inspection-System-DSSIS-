"""把公司標誌上傳到雲端看板（Netlify Blob）。

真實 LOGO 不進公開版控：檔案放在 backend/data/brand-mark.local.png（標誌）與
brand-wordmark.local.png（字標，淺色、供深色看板用），兩者都被 .gitignore 排除。
地端伺服器直接讀這兩個檔；雲端沒有這兩個檔，要用這支工具上傳一次。

    python backend/tools/upload_brand_logo.py

讀 .env.onprem 的 CLOUD_API_URL 與 CLOUD_SYNC_TOKEN（＝站台的 SITE_AGENT_TOKEN）。
換 LOGO 時覆蓋檔案後再跑一次即可；瀏覽器快取一天，看板最晚隔天換新。
"""

from __future__ import annotations

import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend", "onprem"))

from app.envfile import env, load_env      # noqa: E402

NAMES = ("mark", "wordmark")


def main() -> int:
    load_env()
    base = env("CLOUD_API_URL").strip().rstrip("/")
    token = env("CLOUD_SYNC_TOKEN").strip()
    if not base or not token:
        print("未設定 CLOUD_API_URL 或 CLOUD_SYNC_TOKEN（.env.onprem）")
        return 1
    failed = 0
    for name in NAMES:
        path = os.path.join(ROOT, "backend", "data", f"brand-{name}.local.png")
        if not os.path.isfile(path):
            print(f"找不到 {path}，略過")
            failed += 1
            continue
        with open(path, "rb") as f:
            data = f.read()
        r = requests.post(f"{base}/api/v1/ingest/brand/{name}", data=data, timeout=30,
                          headers={"x-agent-token": token, "content-type": "image/png"})
        ok = r.status_code == 200
        print(f"{name}：{'已上傳' if ok else '失敗'}（HTTP {r.status_code}，{len(data):,} bytes）")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
