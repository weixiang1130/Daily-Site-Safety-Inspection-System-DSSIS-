-- 已填單據可修改（2026-10-30 工地回饋）
--
-- 修改＝開新版：舊內容、舊簽名、舊 PDF 整包存進 form_revisions，單據回到簽核中重新
-- 簽名。不就地覆蓋：簽名是對當時的內容負責，內容變了舊簽名不能留著，而稽核要看得到
-- 改過幾次、誰改的、為什麼改。修改原因必填。
-- 舊簽名圖與舊 PDF 留在檔案庫不刪，靠這張表找得回來。

ALTER TABLE inspections   ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE coordinations ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS form_revisions (
  id          SERIAL PRIMARY KEY,
  doc_key     TEXT NOT NULL,               -- 'inspection:12'／'coordination:12'
  rev         INTEGER NOT NULL,            -- 這次修改後是第幾次修改
  reason      TEXT NOT NULL,
  edited_by   INTEGER REFERENCES users(id),
  edited_name TEXT,
  snapshot    JSONB NOT NULL,              -- 修改前的完整內容與簽名名單
  pdf_key     TEXT,                        -- 修改前的 PDF
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS form_revisions_doc ON form_revisions (doc_key, rev);
