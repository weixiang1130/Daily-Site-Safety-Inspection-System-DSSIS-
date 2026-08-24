"""監視器取像（地端直連）。

為什麼地端版比雲端版簡單得多
----------------------------
雲端版（backend/cloud/lib/cctv.ts）要靠一支常駐程式在公司網路內取像、再推到
雲端儲存區，因為監視器主機對來源 IP 有限制：同一時刻由公司網路取像正常
（401 挑戰 → 200），由 Netlify 卻在第一次請求就被回 403，而 Netlify 函式
沒有固定的對外 IP 可以加白名單。

地端伺服器本來就在公司網路內，直接取像即可。推送程式、儲存區、
權杖驗證這一整條路都不需要了——這也是把戰情室搬回地端的好處之一。

設定見 .env.onprem.example 的 CAM_* 區塊。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

from .envfile import env

# 取像逾時。實測一張約 380 KB，正常一兩秒內完成；設上限是為了避免主機沒回應時
# 把 API 的工作執行緒卡住——大螢幕會整個停住，不只是監視器那一格。
TIMEOUT = 15

# 快取秒數。牆上可能同時開好幾個瀏覽器，各自定時更新；沒有快取的話，
# 更新頻率會直接乘上瀏覽器數量打到 NVR。錄影主機的取像能力有限，
# 打太兇會影響到它本來的錄影工作。
CACHE_SEC = 5

_cache: Dict[int, Tuple[float, bytes]] = {}
_lock = threading.Lock()


def channels() -> List[int]:
    """要顯示的頻道。未設定時回空清單，代表停用監視器功能。"""
    out = []
    for part in env("CAM_CHANNELS").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def enabled() -> bool:
    return bool(env("CAM_URL") and channels())


def snapshot(channel: int) -> bytes:
    """取一張畫面。失敗時丟出例外，由呼叫端決定怎麼呈現。"""
    if channel not in channels():
        # 不接受任意頻道：這個值來自網址，放行等於讓外部隨意掃描 NVR 的頻道
        raise ValueError(f"頻道 {channel} 不在允許清單內")

    now = time.time()
    with _lock:
        hit = _cache.get(channel)
        if hit and now - hit[0] < CACHE_SEC:
            return hit[1]

    # 取像放在鎖外面：一次取像要一兩秒，若在鎖內，另一個頻道的請求會被
    # 無謂地擋住。同一頻道同時進來兩個請求最多就是多取一次，可以接受。
    import requests
    from requests.auth import HTTPDigestAuth

    r = requests.get(
        f"{env('CAM_URL').rstrip('/')}/cgi-bin/snapshot.cgi",
        params={"channel": channel},
        auth=HTTPDigestAuth(env("CAM_USER"), env("CAM_PASS")),
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        # 有些機型驗證失敗仍回 200，內容卻是錯誤訊息。照放行的話，
        # 牆上只會出現一格破圖，查不出原因。
        raise RuntimeError(f"回傳非影像內容（{ctype}）：{r.content[:80]!r}")
    if len(r.content) < 1024:
        raise RuntimeError(f"影像過小（{len(r.content)} 位元組），可能不是有效畫面")

    with _lock:
        _cache[channel] = (time.time(), r.content)
    return r.content


def cached_age(channel: int) -> Optional[int]:
    """目前快取影像的秒數，供前端顯示「幾秒前」。沒有快取時回 None。"""
    with _lock:
        hit = _cache.get(channel)
    return int(time.time() - hit[0]) if hit else None
