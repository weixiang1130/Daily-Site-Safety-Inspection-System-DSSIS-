"""環境量測值的危害分級。

這是 backend/cloud/lib/hazard.ts 的地端版本。戰情室搬到地端之後，雲端那份
會隨儀表板一起移除，屆時這裡就是唯一一份——**不要讓兩份長期並存**，
熱危害門檻是法規值，兩份遲早會漂移，而漂移的方向沒有人會發現。

為什麼自己算
------------
廠商平台雖然有「危害等級」欄位，但實測（2026-08-19）在熱指數 49.4 的情況下
仍回報 0。這比沒有數據更危險——工地會誤以為現場安全。因此一律以自己的邏輯
分級，廠商的原始值另存為 vendor_hazard_level，只作為與廠商釐清問題時的佐證。
"""

from __future__ import annotations

import math
from typing import Dict, Optional

# 0 正常｜1 注意｜2 警戒｜3 危險｜4 極度危險
LEVEL_LABEL: Dict[int, str] = {
    0: "正常", 1: "注意", 2: "警戒", 3: "危險", 4: "極度危險",
}


class Threshold:
    def __init__(self, label: str, unit: str, breaks: list,
                 basis: str, caveat: str = ""):
        self.label = label
        self.unit = unit
        self.breaks = breaks        # 由低到高的分界值；落在第 n 個區間即第 n 級
        self.basis = basis
        self.caveat = caveat

    def as_dict(self) -> dict:
        d = {"label": self.label, "unit": self.unit,
             "breaks": self.breaks, "basis": self.basis}
        if self.caveat:
            d["caveat"] = self.caveat
        return d


THRESHOLDS: Dict[str, Threshold] = {
    # 分界直接取自《高氣溫作業熱危害預防指引》（114.6.20 第 2 次修正）附表二，
    # 不可自行四捨五入：熱指數 40.6 依法就是第三級，寫成 41 會少判一級。
    # 四個分界正是美國 NWS 的 80／90／105／130 °F 換算值。
    "heat_index": Threshold(
        "熱指數 體感", "°C", [26.7, 32.2, 40.6, 54.4],
        "職安署《高氣溫作業熱危害預防指引》附表二",
        "體感溫度，非氣溫。由溫度與相對濕度推算，代表人體實際感受到的熱負荷",
    ),
    # 職業安全衛生設施規則第 300 條：8 小時日時量平均音壓級超過 85 分貝應
    # 採取聽力保護措施，90 分貝為 8 小時容許暴露值。
    "noise": Threshold(
        "噪音", "dB", [80, 85, 90],
        "職業安全衛生設施規則第 300 條",
        "為即時音壓級，非 8 小時日時量平均值，不可直接視為法規符合性判定",
    ),
    "pm25": Threshold(
        "PM2.5", "μg/m³", [15.5, 35.5, 54.5],
        "環境部空氣品質指標 AQI（PM2.5 24 小時值）",
        "為即時濃度，AQI 分界原以 24 小時平均值定義",
    ),
    # 工地揚塵多屬粗顆粒，PM10 明顯高於 PM2.5 時通常代表現場揚塵
    "pm10": Threshold(
        "PM10", "μg/m³", [51, 101, 255],
        "環境部空氣品質指標 AQI（PM10 24 小時值）",
        "為即時濃度，AQI 分界原以 24 小時平均值定義",
    ),
    # 溫濕度不單獨分級：熱指數已經同時涵蓋兩者，且熱指數才是能直接對應到
    # 「加強休息、調整工時、停止作業」的指標。breaks 留空代表永遠是正常。
    "temperature": Threshold("溫度", "°C", [], "僅供參考，熱危害判定以熱指數為準"),
    "humidity": Threshold("濕度", "%", [], "僅供參考，熱危害判定以熱指數為準"),
}


def level_of(metric: str, value) -> int:
    """依門檻表判定單一指標的等級。未列管或無效值一律回 0。"""
    t = THRESHOLDS.get(metric)
    if t is None or value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if math.isnan(v) or math.isinf(v):
        return 0
    return min(sum(1 for b in t.breaks if v >= b), 4)


def station_level(metrics: Dict[str, object]) -> int:
    """測站整體危害等級：取各指標中最高者。

    取最高而非平均——任何一項達到危險都必須反映出來，
    不能被其他正常的指標稀釋掉。
    """
    return max((level_of(k, v) for k, v in metrics.items()), default=0)


def heat_index_c(temp_c, rh) -> Optional[float]:
    """由溫度與相對濕度推算熱指數（NWS Rothfusz 迴歸式）。

    廠商若停供熱指數，只要還有溫濕度就能自行算出，不受制於對方。
    迴歸式以華氏定義，因此內部換算後再轉回攝氏。
    """
    try:
        t_c, h = float(temp_c), float(rh)
    except (TypeError, ValueError):
        return None
    if math.isnan(t_c) or math.isnan(h) or not (0 <= h <= 100):
        return None

    t = t_c * 9 / 5 + 32

    # 低溫段迴歸式誤差大，NWS 規定改用簡式，且僅在結果達 80°F 才套用完整式
    simple = 0.5 * (t + 61 + (t - 68) * 1.2 + h * 0.094)
    if (simple + t) / 2 < 80:
        return round((simple - 32) * 5 / 9, 1)

    hi = (-42.379 + 2.04901523 * t + 10.14333127 * h
          - 0.22475541 * t * h - 0.00683783 * t * t - 0.05481717 * h * h
          + 0.00122874 * t * t * h + 0.00085282 * t * h * h
          - 0.00000199 * t * t * h * h)

    # NWS 對乾燥與極濕兩端的修正
    if h < 13 and 80 <= t <= 112:
        hi -= ((13 - h) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif h > 85 and 80 <= t <= 87:
        hi += ((h - 85) / 10) * ((87 - t) / 5)

    return round((hi - 32) * 5 / 9, 1)


def thresholds_payload() -> dict:
    """供前端顯示標籤、單位與判定依據用。"""
    return {k: t.as_dict() for k, t in THRESHOLDS.items()}
