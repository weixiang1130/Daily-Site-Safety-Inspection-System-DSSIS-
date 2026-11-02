-- 填報預設清單：協議巡視表、自主檢查表的廠商／職種／人員／作業項目／作業內容下拉。
--
-- 為什麼另開一張表、不直接 SELECT DISTINCT worklog_reports：LINE 回報的原文不能
-- 直接當選項（廠商欄混著人數與棟別、作業主管有錯字、施作項目混著機具清單），要
-- 清理過；使用者要能新增、也要能把錯的移除，而且移除要記得住——每晚重算不能把
-- 它加回來。
--
-- 內容一律由程式產生，這裡不放任何資料：廠商與人名是公司資料，不進公開版控。
--   source='worklog'：每晚出工回報推送後由雲端依 worklog_reports 重算（清理規則在
--                     backend/cloud/lib/form-options.ts）；n＝近一年出現次數
--   source='form'   ：表單送出時累加；n＝使用次數
--   source='manual' ：現場按「＋ 新增」加的，自動併錯字不動它
-- 同一個值可有多列（不同廠商、不同來源），查詢時合併；vendor 空字串＝不限廠商。
-- 同值只要有一列 active=FALSE 就視為已移除，重新新增才恢復。
-- 改正錯字：舊值 active=FALSE、canonical 記新值。之後 LINE 回報再出現舊寫法，重算時
-- 併到新值、今日名單帶入時也換成新值。已送出的單據不動：送出即已簽核存查。

CREATE TABLE IF NOT EXISTS form_options (
  id         SERIAL PRIMARY KEY,
  site_code  TEXT NOT NULL,
  field      TEXT NOT NULL,                  -- vendor / trade / person / work_item / content
  value      TEXT NOT NULL,
  vendor     TEXT NOT NULL DEFAULT '',
  source     TEXT NOT NULL DEFAULT 'form',
  n          INTEGER NOT NULL DEFAULT 0,
  last_used  DATE,
  active     BOOLEAN NOT NULL DEFAULT TRUE,
  canonical  TEXT,                           -- 被改正成哪個值
  updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (site_code, field, value, vendor, source)
);
CREATE INDEX IF NOT EXISTS form_options_site_field ON form_options (site_code, field);
