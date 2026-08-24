"""探測門禁即時檢視表的結構，作為撰寫收集程式的依據。

為什麼要先探測
--------------
公司開放了門禁資料庫的唯讀檢視表，但欄位結構未知。照猜的欄位寫收集程式，
等於把整支程式建立在假設上——欄位名一錯就整份重寫。先把實際的欄位、型別與
幾筆樣本印出來，再決定怎麼彙總。

在哪裡執行
----------
**必須在連得到資料庫伺服器網段的機器上跑。** 實測（2026-08-21）辦公室
Wi-Fi 網段連不到資料庫網段的 1433 埠：主機 ping 得到、DNS 也解析得到，
但 TCP 不通，應是網段之間的防火牆規則。請在日後要跑戰情室的那台地端主機
上執行，或請資訊單位開放該主機到資料庫的 1433。

設定
----
連線資訊放在專案根目錄的 .env.onprem（已被 .gitignore 排除，不會進版控）：

    ACCESS_DB_HOST=...
    ACCESS_DB_NAME=...
    ACCESS_DB_USER=...
    ACCESS_DB_PASS=...
    ACCESS_DB_VIEW=...
    ACCESS_SITE_LIKE=...      # 用來篩出本工地的 LocationName 關鍵字

用法
----
    python tools/inspect_access_view.py

輸出欄位清單、型別、總筆數，以及去識別化後的樣本列。
樣本會遮蔽看起來像姓名或證號的欄位，避免把個資貼進聊天或文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import pyodbc
except ImportError:
    sys.exit("需要 pyodbc，請先執行：pip install pyodbc")

ENV_FILE = Path(__file__).resolve().parent.parent / ".env.onprem"

# 這些欄位一律遮蔽。門禁資料含姓名與員工編號，屬第三人個資，
# 探測結構不需要看到真實內容。
SENSITIVE_HINTS = ("name", "employee", "emp", "card", "id_no", "idno",
                   "person", "user", "tel", "phone", "姓名", "工號")


def load_env() -> None:
    """讀取本機設定。刻意不用第三方套件，少一個相依。"""
    if not ENV_FILE.exists():
        sys.exit(f"找不到 {ENV_FILE.name}，請先建立（格式見本檔開頭說明）。")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def connect() -> "pyodbc.Connection":
    host = os.environ["ACCESS_DB_HOST"]
    name = os.environ["ACCESS_DB_NAME"]
    # 依序嘗試已安裝的驅動程式；18 版預設要求加密，因此明確關閉憑證驗證
    for drv in ("ODBC Driver 18 for SQL Server",
                "ODBC Driver 17 for SQL Server",
                "SQL Server"):
        cs = (f"DRIVER={{{drv}}};SERVER={host};DATABASE={name};"
              f"UID={os.environ['ACCESS_DB_USER']};PWD={os.environ['ACCESS_DB_PASS']};"
              "TrustServerCertificate=yes;Connection Timeout=10")
        try:
            return pyodbc.connect(cs, timeout=10)
        except pyodbc.Error as e:
            print(f"  [{drv}] 連線失敗：{str(e)[:110]}")
    sys.exit("所有驅動程式都連不上。請確認這台機器連得到資料庫網段的 1433 埠。")


def mask(column: str, value) -> str:
    if value is None:
        return "NULL"
    low = column.lower()
    if any(h in low for h in SENSITIVE_HINTS):
        s = str(value)
        return f"<遮蔽 {len(s)} 字>"
    return str(value)[:48]


def main() -> None:
    load_env()
    view = os.environ["ACCESS_DB_VIEW"]
    site_like = os.environ.get("ACCESS_SITE_LIKE", "")

    print(f"連線 {os.environ['ACCESS_DB_HOST']} / {os.environ['ACCESS_DB_NAME']}")
    conn = connect()
    cur = conn.cursor()

    print(f"\n=== {view} 欄位 ===")
    cur.execute(f"SELECT TOP 0 * FROM {view}")
    columns = [d[0] for d in cur.description]
    for d in cur.description:
        print(f"  {d[0]:<28} {d[1].__name__:<10} 長度={d[3]} 可為空={d[6]}")

    cur.execute(f"SELECT COUNT(*) FROM {view}")
    print(f"\n總筆數：{cur.fetchone()[0]}")

    # 這張檢視表涵蓋全公司列管工地，先看看有哪些地點，才知道怎麼篩本工地
    loc_col = next((c for c in columns if c.lower() == "locationname"), None)
    if loc_col:
        print(f"\n=== {loc_col} 的相異值（前 30）===")
        cur.execute(f"SELECT {loc_col}, COUNT(*) AS n FROM {view} "
                    f"GROUP BY {loc_col} ORDER BY n DESC")
        for i, row in enumerate(cur.fetchall()[:30]):
            print(f"  {row[1]:>6}  {row[0]}")

    print("\n=== 樣本列（姓名／證號類欄位已遮蔽）===")
    where = f" WHERE {loc_col} LIKE ?" if (loc_col and site_like) else ""
    sql = f"SELECT TOP 3 * FROM {view}{where}"
    cur.execute(sql, (f"%{site_like}%",)) if where else cur.execute(sql)
    for n, row in enumerate(cur.fetchall(), 1):
        print(f"--- 第 {n} 列 ---")
        for col, val in zip(columns, row):
            print(f"  {col:<28} {mask(col, val)}")

    conn.close()
    print("\n完成。請把上面的欄位清單貼回來，我依實際結構寫收集程式。")


if __name__ == "__main__":
    main()
