# -*- coding: utf-8 -*-
"""在 SQL Server LocalDB 上建立 SafetyOps 資料庫。

用法：
    python tools/create_db.py            # 建立（已存在則略過）
    python tools/create_db.py --drop     # 先刪除再重建（會清空所有資料！）
"""
import sys

import pyodbc

SERVER = r"(localdb)\MSSQLLocalDB"
DBNAME = "SafetyOps"
DRIVER = "ODBC Driver 17 for SQL Server"

master_cs = (f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE=master;"
             f"Trusted_Connection=yes;")


def main():
    cn = pyodbc.connect(master_cs, autocommit=True)
    cu = cn.cursor()

    cu.execute("SELECT name FROM sys.databases ORDER BY name")
    print("現有資料庫：", ", ".join(r[0] for r in cu.fetchall()))

    if "--drop" in sys.argv:
        cu.execute(f"IF DB_ID('{DBNAME}') IS NOT NULL "
                   f"BEGIN ALTER DATABASE [{DBNAME}] SET SINGLE_USER "
                   f"WITH ROLLBACK IMMEDIATE; DROP DATABASE [{DBNAME}]; END")
        print(f"已刪除 {DBNAME}")

    cu.execute(f"IF DB_ID('{DBNAME}') IS NULL CREATE DATABASE [{DBNAME}]")
    cu.execute("SELECT name, collation_name FROM sys.databases WHERE name = ?", DBNAME)
    for name, coll in cu.fetchall():
        print(f"資料庫 {name} 就緒，定序 {coll}")

    print("SQL Server 版本：", cn.getinfo(pyodbc.SQL_DBMS_VER))
    cn.close()

    print("\n連線字串（設定到 DATABASE_URL 環境變數）：")
    print(f'  mssql+pyodbc://@{SERVER.replace(chr(92), "%5C")}/{DBNAME}'
          f'?driver={DRIVER.replace(" ", "+")}&trusted_connection=yes')


if __name__ == "__main__":
    main()
