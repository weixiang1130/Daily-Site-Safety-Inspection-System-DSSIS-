"""工地自行維護的看板設定；雲端為已連線檢視器的唯一設定來源。"""
import json
import re
from datetime import date
from urllib.parse import urlparse


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("設定格式錯誤")
    out = {}
    fields = {
        "contacts": (12, {"role": 32, "name": 64, "phone": 40}),
        "schedule": (24, {"start": 5, "end": 5, "label": 40}),
        "announcements": (20, {"title": 80, "body": 600, "source": 100,
                                "url": 500, "start_date": 10, "end_date": 10}),
    }
    occupied = set()
    for key, (limit, columns) in fields.items():
        rows = value.get(key, [])
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError(f"{key} 筆數超過上限或格式錯誤")
        out[key] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("欄位格式錯誤")
            cleaned = {}
            for field, size in columns.items():
                text = row.get(field, "")
                if not isinstance(text, str) or len(text.strip()) > size:
                    raise ValueError(f"{field} 格式或長度錯誤")
                cleaned[field] = text.strip()
            if key == "contacts" and not all(cleaned[k] for k in ("role", "name", "phone")):
                raise ValueError("聯絡人職務、姓名與電話必填")
            if key == "schedule":
                if not cleaned["label"]:
                    raise ValueError("作業名稱必填")
                times = []
                for field in ("start", "end"):
                    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cleaned[field]):
                        raise ValueError("時間請使用 HH:MM")
                    h, m = map(int, cleaned[field].split(":"))
                    times.append(h * 60 + m)
                start, end = times
                if start == end:
                    raise ValueError("開始與結束時間不可相同")
                minutes = set(range(start, end)) if end > start else set(range(start, 1440)) | set(range(end))
                if occupied & minutes:
                    raise ValueError("作業時段不可重疊")
                occupied |= minutes
            if key == "announcements":
                if not cleaned["title"] or not cleaned["body"]:
                    raise ValueError("公告標題與內容必填")
                for field in ("start_date", "end_date"):
                    if cleaned[field]:
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned[field]):
                            raise ValueError("公告日期格式錯誤")
                        date.fromisoformat(cleaned[field])
                if cleaned["start_date"] and cleaned["end_date"] and cleaned["end_date"] < cleaned["start_date"]:
                    raise ValueError("公告結束日期不可早於開始日期")
                if cleaned["url"]:
                    u = urlparse(cleaned["url"])
                    if u.scheme != "https" or not u.hostname or u.username or u.password:
                        raise ValueError("來源網址須為 HTTPS 且不可含帳密")
            out[key].append(cleaned)
    out["schedule"].sort(key=lambda x: x["start"])

    # 無災害紀錄的起算設定。天數自起算日逐日累計，但「從哪一天起算」
    # 與「起算前已累計的天數」系統無從得知，由工地填。
    safety = value.get("safety") or {}
    if not isinstance(safety, dict):
        raise ValueError("無災害設定格式錯誤")
    start = safety.get("start_date", "")
    days = safety.get("base_days", "")
    if not isinstance(start, str) or not isinstance(days, str):
        raise ValueError("無災害設定格式錯誤")
    start, days = start.strip(), days.strip()
    if start:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            raise ValueError("無災害起算日格式錯誤")
        date.fromisoformat(start)
    if days and not re.fullmatch(r"\d{1,7}", days):
        raise ValueError("起算前累計天數請填整數")
    out["safety"] = {"start_date": start, "base_days": days}
    return out


def board_payload(site):
    config = json.loads(site.board_config) if site.board_config else {}
    config.setdefault("contacts", [])
    config.setdefault("schedule", [])
    config.setdefault("announcements", [])
    config.setdefault("safety", {"start_date": "", "base_days": ""})
    return {"site_id": site.id, "site_code": site.code, "site_name": site.name,
            "revision": site.board_revision or 0, "config": config}
