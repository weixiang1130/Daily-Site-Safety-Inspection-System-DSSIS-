-- 出工資料庫：地端副本的增量匯出（/api/v1/export/worklog）以（更新時間, id）為游標排序。
-- 獨立成新的 migration：008 已在分支部署套用過，Netlify 不會重跑已套用的 migration，
-- 把索引補寫進 008 會被略過。
CREATE INDEX IF NOT EXISTS worklog_reports_updated_idx ON worklog_reports (updated_at, id);
