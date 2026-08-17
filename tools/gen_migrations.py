# -*- coding: utf-8 -*-
"""產生 Netlify Database（Postgres）的 migration SQL。

Netlify 會在部署時自動套用 netlify/database/migrations/ 下的 SQL。
本腳本由 data/forms.json 與 app/seed.py 的主檔定義產生：

    001_init          資料表與索引
    002_seed_forms    28 張檢查表模板與 541 個檢查項目
    003_seed_master   工地、廠商、使用者（密碼雜湊與 Python 版同格式）

用法：
    python tools/gen_migrations.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend', 'onprem'))

from app.auth import hash_password          # noqa: E402
from app.seed import SITES, USERS, VENDORS  # noqa: E402

BASE_DIR = ROOT
MIG_DIR = os.path.join(BASE_DIR, "netlify", "database", "migrations")
FORMS_JSON = os.path.join(BASE_DIR, "data", "forms.json")


def q(v):
    """SQL 字串常值。None → NULL。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def write(name, sql):
    d = os.path.join(MIG_DIR, name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "migration.sql")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(sql)
    kb = os.path.getsize(path) / 1024
    print(f"  {name}/migration.sql　{kb:.1f} KB")


INIT_SQL = """-- 職安填報系統 —— 資料表定義
-- 由 tools/gen_migrations.py 產生，請勿手動編輯。
--
-- 對應 app/db.py 的 SQLAlchemy 模型。內網版使用 SQL Server，
-- 雲端版使用 Postgres，兩者欄位與語意一致，僅型別名稱不同：
--   NVARCHAR(n)    → TEXT
--   NVARCHAR(MAX)  → TEXT
--   DATETIME       → TIMESTAMPTZ
--   BIT            → BOOLEAN

CREATE TABLE IF NOT EXISTS sites (
  id      SERIAL PRIMARY KEY,
  code    TEXT NOT NULL UNIQUE,
  name    TEXT NOT NULL,
  active  BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS vendors (
  id      SERIAL PRIMARY KEY,
  code    TEXT NOT NULL UNIQUE,
  name    TEXT NOT NULL,
  active  BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS users (
  id            SERIAL PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  employee_no   TEXT,
  role          TEXT NOT NULL DEFAULT 'inspector',
  site_id       INTEGER REFERENCES sites(id),
  active        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS form_templates (
  form_code   TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  short_name  TEXT,
  form_type   TEXT,
  item_count  INTEGER NOT NULL DEFAULT 0,
  active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS form_items (
  id           SERIAL PRIMARY KEY,
  form_code    TEXT NOT NULL REFERENCES form_templates(form_code),
  seq          INTEGER NOT NULL,
  category     TEXT,
  hazard_code  TEXT,
  hazard_label TEXT,
  text         TEXT NOT NULL,
  UNIQUE (form_code, seq)
);

CREATE TABLE IF NOT EXISTS inspections (
  id           SERIAL PRIMARY KEY,
  site_id      INTEGER NOT NULL REFERENCES sites(id),
  form_code    TEXT NOT NULL REFERENCES form_templates(form_code),
  inspect_date DATE NOT NULL DEFAULT CURRENT_DATE,
  location     TEXT,
  weather      TEXT,
  inspector_id INTEGER NOT NULL REFERENCES users(id),
  status       TEXT NOT NULL DEFAULT 'draft',
  submitted_at TIMESTAMPTZ,
  approved_at  TIMESTAMPTZ,
  pdf_key      TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_inspections_date_site
  ON inspections (inspect_date, site_id);

CREATE TABLE IF NOT EXISTS inspection_results (
  id            SERIAL PRIMARY KEY,
  inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
  item_id       INTEGER NOT NULL REFERENCES form_items(id),
  day           INTEGER,
  result        TEXT NOT NULL,
  remark        TEXT
);
CREATE INDEX IF NOT EXISTS ix_results_inspection
  ON inspection_results (inspection_id);

CREATE TABLE IF NOT EXISTS coordinations (
  id             SERIAL PRIMARY KEY,
  site_id        INTEGER NOT NULL REFERENCES sites(id),
  meeting_date   DATE NOT NULL DEFAULT CURRENT_DATE,
  work_date      DATE NOT NULL DEFAULT CURRENT_DATE,
  weather        TEXT,
  agreement_text TEXT,
  patrol_text    TEXT,
  handling_text  TEXT,
  status         TEXT NOT NULL DEFAULT 'draft',
  submitted_at   TIMESTAMPTZ,
  pdf_key        TEXT,
  created_by     INTEGER REFERENCES users(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_coordinations_date_site
  ON coordinations (work_date, site_id);

CREATE TABLE IF NOT EXISTS coordination_attendees (
  id              SERIAL PRIMARY KEY,
  coordination_id INTEGER NOT NULL REFERENCES coordinations(id) ON DELETE CASCADE,
  work_item       TEXT,
  vendor_id       INTEGER REFERENCES vendors(id),
  vendor_name     TEXT,
  trade           TEXT,
  person_name     TEXT,
  employee_no     TEXT,
  work_content    TEXT
);

CREATE TABLE IF NOT EXISTS findings (
  id                 SERIAL PRIMARY KEY,
  site_id            INTEGER NOT NULL REFERENCES sites(id),
  inspection_id      INTEGER REFERENCES inspections(id),
  coordination_id    INTEGER REFERENCES coordinations(id),
  item_id            INTEGER REFERENCES form_items(id),
  source             TEXT NOT NULL DEFAULT 'inspection',
  found_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  location           TEXT,
  hazard_code        TEXT,
  hazard_label       TEXT,
  description        TEXT NOT NULL,
  vendor_id          INTEGER REFERENCES vendors(id),
  responsible_person TEXT,
  severity           TEXT NOT NULL DEFAULT 'minor',
  action_type        TEXT NOT NULL DEFAULT 'onsite',
  due_date           DATE,
  fixed_at           TIMESTAMPTZ,
  fix_note           TEXT,
  verifier_id        INTEGER REFERENCES users(id),
  verified_at        TIMESTAMPTZ,
  status             TEXT NOT NULL DEFAULT 'open',
  penalty            NUMERIC(12,2),
  photo_before       TEXT,
  photo_after        TEXT,
  created_by         INTEGER REFERENCES users(id),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_findings_found_site  ON findings (found_at, site_id);
CREATE INDEX IF NOT EXISTS ix_findings_status_due  ON findings (status, due_date);
CREATE INDEX IF NOT EXISTS ix_findings_hazard      ON findings (hazard_code);
CREATE INDEX IF NOT EXISTS ix_findings_vendor      ON findings (vendor_id);

CREATE TABLE IF NOT EXISTS signatures (
  id              SERIAL PRIMARY KEY,
  inspection_id   INTEGER REFERENCES inspections(id) ON DELETE CASCADE,
  coordination_id INTEGER REFERENCES coordinations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,
  signer_id       INTEGER REFERENCES users(id),
  signer_name     TEXT NOT NULL,
  image_key       TEXT NOT NULL,
  signed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  signed_ip       TEXT
);

CREATE TABLE IF NOT EXISTS device_readings (
  id          SERIAL PRIMARY KEY,
  site_id     INTEGER REFERENCES sites(id),
  site_code   TEXT,
  vendor_code TEXT,
  device_type TEXT,
  device_id   TEXT,
  metric      TEXT,
  value_num   NUMERIC(18,4),
  value_text  TEXT,
  reading_at  TIMESTAMPTZ NOT NULL,
  raw_payload TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_device_site_type_time
  ON device_readings (site_code, device_type, reading_at);
"""


def gen_forms():
    with open(FORMS_JSON, encoding="utf-8") as f:
        forms = json.load(f)["forms"]

    out = ["-- 28 張自主檢查表模板與檢查項目",
           "-- 由 tools/gen_migrations.py 依 data/forms.json 產生，請勿手動編輯。", ""]

    rows = ",\n".join(
        f"  ({q(f['form_code'])}, {q(f['title'])}, {q(f['short_name'])}, "
        f"{q(f['form_type'])}, {f['item_count']})" for f in forms)
    out.append("INSERT INTO form_templates "
               "(form_code, title, short_name, form_type, item_count) VALUES")
    out.append(rows)
    out.append("ON CONFLICT (form_code) DO UPDATE SET")
    out.append("  title = EXCLUDED.title, short_name = EXCLUDED.short_name,")
    out.append("  form_type = EXCLUDED.form_type, item_count = EXCLUDED.item_count;")
    out.append("")

    item_rows = []
    for f in forms:
        for it in f["items"]:
            item_rows.append(
                f"  ({q(f['form_code'])}, {it['seq']}, {q(it['category'])}, "
                f"{q(it['hazard_code'])}, {q(it['hazard_label'])}, {q(it['text'])})")
    out.append("INSERT INTO form_items "
               "(form_code, seq, category, hazard_code, hazard_label, text) VALUES")
    out.append(",\n".join(item_rows))
    out.append("ON CONFLICT (form_code, seq) DO UPDATE SET")
    out.append("  category = EXCLUDED.category, hazard_code = EXCLUDED.hazard_code,")
    out.append("  hazard_label = EXCLUDED.hazard_label, text = EXCLUDED.text;")
    out.append("")

    n_items = sum(f["item_count"] for f in forms)
    print(f"  表單：{len(forms)} 張、{n_items} 項")
    return "\n".join(out)


def gen_master():
    out = ["-- 工地、廠商、使用者主檔",
           "-- 由 tools/gen_migrations.py 產生，請勿手動編輯。",
           "-- 密碼雜湊格式與 Python 版 app/auth.py 相同（PBKDF2-SHA256），",
           "-- TypeScript 端以 Web Crypto 驗證，兩邊可互通。", ""]

    out.append("INSERT INTO sites (code, name) VALUES")
    out.append(",\n".join(f"  ({q(c)}, {q(n)})" for c, n in SITES))
    out.append("ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;")
    out.append("")

    out.append("INSERT INTO vendors (code, name) VALUES")
    out.append(",\n".join(f"  ({q(c)}, {q(n)})" for c, n in VENDORS))
    out.append("ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;")
    out.append("")

    out.append("INSERT INTO users "
               "(username, password_hash, display_name, employee_no, role, site_id) VALUES")
    rows = []
    for username, pw, name, role, emp, site_code in USERS:
        site = (f"(SELECT id FROM sites WHERE code = {q(site_code)})"
                if site_code else "NULL")
        rows.append(f"  ({q(username)}, {q(hash_password(pw))}, {q(name)}, "
                    f"{q(emp)}, {q(role)}, {site})")
    out.append(",\n".join(rows))
    out.append("ON CONFLICT (username) DO UPDATE SET")
    out.append("  display_name = EXCLUDED.display_name, role = EXCLUDED.role,")
    out.append("  employee_no = EXCLUDED.employee_no, site_id = EXCLUDED.site_id;")
    out.append("")

    print(f"  主檔：{len(SITES)} 工地、{len(VENDORS)} 廠商、{len(USERS)} 帳號")
    return "\n".join(out)


def main():
    os.makedirs(MIG_DIR, exist_ok=True)
    print("產生 migration：")
    write("001_init", INIT_SQL)
    write("002_seed-forms", gen_forms())
    write("003_seed-master", gen_master())
    print("\n完成。Netlify 部署時會自動依序套用。")
    print("注意：預設密碼僅供試辦，上線前務必於系統內更換。")


if __name__ == "__main__":
    main()
