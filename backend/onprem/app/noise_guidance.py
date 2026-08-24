"""噪音風險等級對應的應辦措施。

依據
----
職業安全衛生設施規則第 300 條、第 300 條之 1，
勞工作業環境監測實施辦法，勞工健康保護規則。

為什麼要把措施寫進系統
----------------------
跟熱危害同一個道理：牆上顯示「噪音 87 dB」對現場沒有用——值班人員未必知道
85 分貝就已經觸發聽力保護計畫的法定義務。要直接講出「現在該做哪幾件事」。

**一個必須一直記得的限制**
--------------------------
法規講的是「8 小時日時量平均音壓級」，而我們量到的是**即時音壓級**，
兩者不能畫等號：即時 90 dB 不代表日時量平均就是 90 dB。

因此本模組的措辭一律是「應評估、應確認」，**不寫「違反規定」**。
牆上出現一則法規判定，現場會當真；判錯的代價是有人依此認定不必戴防護具，
或反過來被誤指違法。要下符合性判定，得靠作業環境監測的正式量測。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .hazard import THRESHOLDS, level_of

# 各級名稱。對應 hazard.py 的 noise 分界 [80, 85, 90, 115]
_NAME = {
    0: "未達管制值",
    1: "注意",
    2: "應啟動聽力保護",
    3: "超過容許暴露值",
    4: "超過最高音壓限值",
}

_PRINCIPLE = {
    0: "低於噪音管制相關規定之門檻，維持一般作業管理即可。",
    1: "已接近聽力保護措施門檻，應留意勞工實際暴露時間。",
    2: "已達設施規則第 300 條之 1 應採聽力保護措施之水準。",
    3: "已達第 300 條 8 小時日時量平均容許值，應採工程控制或縮短暴露時間。",
    4: "第 300 條規定連續性噪音最高不得超過 115 分貝，應立即使勞工退離。",
}

# 各級相較前一級「多」要求的事。牆上空間有限，一次只顯示這一級的重點。
_ADDED: Dict[int, List[str]] = {
    0: [],
    1: [
        "標示噪音作業區域，提醒進入者留意",
        "提供防音防護具供勞工視需要使用",
        "留意勞工每日實際暴露時間",
    ],
    2: [
        "實施聽力保護措施並作成紀錄留存三年："
        "噪音危害控制、防音防護具選用及佩戴、聽力保護教育訓練、"
        "健康檢查及管理、成效評估及改善",
        "確認本作業場所已納入每六個月一次之作業環境監測",
        "屬特別危害健康作業，應安排特殊健康檢查並依結果分級管理",
        "勞工人數達一百人以上者，聽力保護措施應由專業人員或機構規劃",
    ],
    3: [
        "優先採取工程控制：消音、隔音、吸音、減振或改用低噪音機具",
        "工程控制未能達成前，應縮短勞工暴露時間至容許範圍內",
        "確實使勞工佩戴有效之防音防護具",
    ],
    4: [
        "立即使勞工退離該區域，非經改善不得繼續作業",
        "衝擊性噪音尖峰值亦不得超過 140 分貝",
    ],
}


def _measures(level: int) -> List[str]:
    out: List[str] = []
    for i in range(1, level + 1):
        out.extend(_ADDED[i])
    return out


def allowed_hours(db_value: float) -> Optional[float]:
    """在此音壓級下，第 300 條容許的每日暴露時數。

    附表的對照關係為 90 dB／8 小時，每增加 5 分貝，容許時間減半
    （90-8、95-4、100-2、105-1、110-½、115-¼），即 T = 8 / 2^((L-90)/5)。
    用算式而不是抄一張表：現場量到的是 93.4 這種值，查表只能落在區間，
    算式才答得出「這個音量還能待多久」。

    未達 90 分貝回 None——第 300 條的表格自 90 分貝起算，
    低於此值硬套算式會得出「可暴露 20 小時」這種沒有意義的數字。
    """
    try:
        v = float(db_value)
    except (TypeError, ValueError):
        return None
    if v < 90:
        return None
    hours = 8 / (2 ** ((v - 90) / 5))
    return round(hours, 2)


def scale() -> List[dict]:
    """各級的界線與名稱，供牆上顯示級距刻度。理由同 heat_guidance.scale()。"""
    breaks = THRESHOLDS["noise"].breaks
    out = []
    for lvl in range(len(breaks) + 1):
        out.append({
            "level": lvl,
            "name": _NAME[lvl],
            "from": breaks[lvl - 1] if lvl > 0 else None,
            "to": breaks[lvl] if lvl < len(breaks) else None,
        })
    return out


def noise_guidance(db_value) -> Optional[dict]:
    """依即時音壓級取得該級的應辦措施。無有效值時回 None。"""
    if db_value is None:
        return None
    level = level_of("noise", db_value)
    if level == 0:
        # 未達門檻不佔版面。常態顯示的警示很快就會被當成背景而失去作用。
        return None

    hours = allowed_hours(db_value)
    return {
        "level": level,
        "name": _NAME[level],
        "principle": _PRINCIPLE[level],
        "value": float(db_value),
        "measures": _measures(level),
        "focus": _ADDED[level],
        "allowed_hours": hours,
        "scale": scale(),
        "basis": THRESHOLDS["noise"].basis,
        "unit": THRESHOLDS["noise"].unit,
        # 這句話會直接出現在牆上，措辭必須守住「即時值 ≠ 日時量平均」
        "caveat": "本數值為即時音壓級，非 8 小時日時量平均值；"
                  "是否符合法規應以作業環境監測結果認定。",
    }


# 廠商提供的營建工程噪音時段警報旗標 → 顯示名稱。
#
# 這是**環保**範疇（噪音管制法／營建工程噪音管制標準，管的是工地周界對外噪音），
# 與上面的職安聽力保護是兩回事，因此不併入危害等級計算，只在牆上另外標示。
# 混在一起會讓現場以為「環保沒超標＝勞工聽力沒問題」。
PERIOD_LABEL = {
    "noise_alarm_day": "日間",
    "noise_alarm_evening": "晚間",
    "noise_alarm_night": "夜間",
}


def period_alarms(metrics: Dict[str, object]) -> List[str]:
    """目前有哪些時段的營建噪音管制警報成立。"""
    out = []
    for key, label in PERIOD_LABEL.items():
        v = metrics.get(key)
        try:
            if v is not None and float(v) > 0:
                out.append(label)
        except (TypeError, ValueError):
            continue
    return out
