-- 職安填報系統 —— 資料表定義
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
