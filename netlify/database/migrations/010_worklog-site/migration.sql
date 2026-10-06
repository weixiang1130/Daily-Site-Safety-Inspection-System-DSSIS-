-- 出工資料庫多工地：除了主場站，還收其他工地的出工回報（各自一個來源）。
-- 原本的唯一鍵（日期, 棟別, 廠商）不含工地：兩個工地同一天、同一家廠商、都沒寫
-- 棟別時會互相覆蓋。改成（工地, 日期, 棟別, 廠商）。
--
-- 既有資料全部是主場站 BD04 的。回填時同時推進 updated_at：地端副本
-- 以（更新時間, id）增量同步，不推進的話這些舊列永遠不會再被送下去、地端的
-- 工地代碼會一直是空的。
--
-- 唯一性用 unique index 而非 constraint：CREATE UNIQUE INDEX 可寫 IF NOT EXISTS，
-- 重跑不會失敗；ON CONFLICT (欄位…) 對兩者一樣有效。

ALTER TABLE worklog_reports ADD COLUMN IF NOT EXISTS site_code TEXT NOT NULL DEFAULT '';
UPDATE worklog_reports SET site_code = 'BD04', updated_at = NOW() WHERE site_code = '';

ALTER TABLE worklog_reports DROP CONSTRAINT IF EXISTS worklog_reports_report_date_building_vendor_key;
CREATE UNIQUE INDEX IF NOT EXISTS worklog_reports_site_key
  ON worklog_reports (site_code, report_date, building, vendor);
CREATE INDEX IF NOT EXISTS worklog_reports_site_date ON worklog_reports (site_code, report_date);

-- 解析失敗清單也分工地，否則三個工地的失敗訊息混在一起沒法看
ALTER TABLE worklog_rejects ADD COLUMN IF NOT EXISTS site_code TEXT NOT NULL DEFAULT '';
UPDATE worklog_rejects SET site_code = 'BD04' WHERE site_code = '';
