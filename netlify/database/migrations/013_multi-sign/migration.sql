-- 多人分次簽核（2026-10-30 工地回饋）
--
-- 原本一張單只有一格簽名、選角色就能簽，簽完等於代表任何人。改成：
--   填完 → 儲存（status='signing'，內容鎖定）→ 逐格簽名，每簽一格就存、可中途離開、
--   下一位換裝置也接得上 → 完成簽核（status='submitted'）才產生 PDF。
-- 內容要鎖定：簽名是對這份內容負責，簽完還能改就對不上了。
--
-- sign_slots：這張單要簽的格子 [{key, role, name}]。角色各一格，參加協議的廠商各一格
--            （name 預帶負責人）。舊單為 NULL，PDF 照舊。
-- signatures：doc_key＝'coordination:12'，加 slot_key 後一格只能簽一次（唯一索引；舊資料
--            兩欄皆 NULL，不受影響）。image_hash 用來擋同一份單出現一模一樣的簽名圖。

ALTER TABLE inspections   ADD COLUMN IF NOT EXISTS sign_slots JSONB;
ALTER TABLE coordinations ADD COLUMN IF NOT EXISTS sign_slots JSONB;

ALTER TABLE signatures ADD COLUMN IF NOT EXISTS doc_key TEXT;
ALTER TABLE signatures ADD COLUMN IF NOT EXISTS slot_key TEXT;
ALTER TABLE signatures ADD COLUMN IF NOT EXISTS image_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS signatures_doc_slot ON signatures (doc_key, slot_key);
