-- 地端同步看得到所有異動（2026-11-02 全流程盤點）
--
-- 匯出原本只看建立、送出、改善、覆驗的時間。覆驗退回會把 fixed_at 清掉、修改內容和
-- 作廢不動這些時間，地端就永遠停在舊狀態。改用資料庫觸發器維護 updated_at：不靠每個
-- UPDATE 都記得寫，新增的流程也不會漏。既有資料的 updated_at 是遷移當下，上線後第一輪
-- 同步會重送一次，地端順便補齊新增欄位（責任人、處置、改善說明、單據關聯）。

ALTER TABLE findings      ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE inspections   ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE coordinations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- 自主檢查表的受檢廠商：當天有回報的廠商接受檢查並簽名
ALTER TABLE inspections ADD COLUMN IF NOT EXISTS vendor_name TEXT;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS findings_touch ON findings;
CREATE TRIGGER findings_touch BEFORE UPDATE ON findings
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
DROP TRIGGER IF EXISTS inspections_touch ON inspections;
CREATE TRIGGER inspections_touch BEFORE UPDATE ON inspections
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
DROP TRIGGER IF EXISTS coordinations_touch ON coordinations;
CREATE TRIGGER coordinations_touch BEFORE UPDATE ON coordinations
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE INDEX IF NOT EXISTS idx_findings_updated_at      ON findings (updated_at);
CREATE INDEX IF NOT EXISTS idx_inspections_updated_at   ON inspections (updated_at);
CREATE INDEX IF NOT EXISTS idx_coordinations_updated_at ON coordinations (updated_at);
