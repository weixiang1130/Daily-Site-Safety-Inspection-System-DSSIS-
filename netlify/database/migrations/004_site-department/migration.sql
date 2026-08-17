-- 工地隸屬的事業處（處級）。
-- 公司工地依體系表分屬數個事業處，儀表板與填報選單都需要依此分組，
-- 否則四十多個工地擠在同一個下拉選單裡，現場很難找到自己的工地。
--
-- 實際的處級與工地名稱屬公司資料，不寫在 migration 裡，
-- 由 tools/import_sites.py 於執行時透過 API 匯入。

ALTER TABLE sites ADD COLUMN IF NOT EXISTS department TEXT;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_sites_department ON sites (department, sort_order);
