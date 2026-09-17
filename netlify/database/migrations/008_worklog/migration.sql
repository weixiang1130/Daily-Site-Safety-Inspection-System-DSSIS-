-- 出工資料庫：工務所 LINE 群組「出工回報」的歷史明細，供人力與工種分析。
--
-- 資料由 GitHub Actions 每天 00:07（台北）解析出工回報試算表後推上來
-- （backend/cloud-runner/push_worklog.py → /api/v1/ingest/worklog），
-- 不在白天寫入：資料庫按「醒著的時數」計費，排在看板每日第一次讀取
-- 喚醒資料庫的同一時段，通常不增加喚醒次數。
--
-- 為什麼要存：看板只顯示「今天」，歷史原本只存在來源試算表裡——它曾被
-- 移到垃圾桶，若被截斷或換表，歷史就沒了。這裡是出工歷史的正本。
--
-- 更新方式：每晚重送最近 14 天，以 (日期, 棟別, 廠商) upsert，事後修改
-- 的回報會被更新；**來源裡消失的列不會被刪除**（來源被截斷時不能跟著丟
-- 歷史）。同一則 LINE 訊息因解析規則改進而改判到不同鍵時，刪掉舊鍵那筆。
--
-- 個資：廠商、作業主管、回報人（LINE 顯示名稱）與原文屬公司資料，只能
-- 經登入後的匯出端點取得；不存 LINE 使用者 ID。

-- 出工回報：一家廠商、一棟、一天一筆
CREATE TABLE IF NOT EXISTS worklog_reports (
  id            SERIAL PRIMARY KEY,
  report_date   DATE NOT NULL,
  building      TEXT NOT NULL DEFAULT '',     -- 空字串＝回報未填棟別（NULL 無法參與唯一鍵）
  vendor        TEXT NOT NULL,
  headcount     INTEGER,                      -- 總人數（總人數／合計 → 出工數加總 → 工種加總）
  trade_summary TEXT,                         -- 看板用的工種摘要，如「電班8、水班1」
  supervisor    TEXT,
  tasks         TEXT,
  reporter      TEXT,
  reported_at   TIMESTAMP,                    -- LINE 訊息接收時間（台北時間）
  message_id    TEXT,
  raw           TEXT,                         -- 原文：解析規則改進時可整批重算
  created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (report_date, building, vendor)
);
CREATE INDEX IF NOT EXISTS worklog_reports_date ON worklog_reports (report_date);
CREATE INDEX IF NOT EXISTS worklog_reports_message ON worklog_reports (message_id);

-- 工種明細：一筆回報拆成多個工種。只有訊息逐項寫了人數才有（實測約四成），
-- 且加總不一定等於總人數（總人數常含工程師、或只列部分工種）——分析時
-- 總人數看 worklog_reports，工種結構看這張。
CREATE TABLE IF NOT EXISTS worklog_trades (
  report_id  INTEGER NOT NULL REFERENCES worklog_reports(id) ON DELETE CASCADE,
  trade      TEXT NOT NULL,                   -- 訊息原寫法，如「電工班」「電銲工」
  headcount  INTEGER NOT NULL,
  PRIMARY KEY (report_id, trade)
);

-- 工種歸類：同一工種有多種寫法（電班／電工班、電焊工／電銲工），也有把
-- 作業項目當工種寫的（「B區版筋綁紮：5人」）。由人判斷後維護，匯出時
-- 以歸類結果呈現，沒對到的沿用原寫法。
CREATE TABLE IF NOT EXISTS worklog_trade_aliases (
  trade       TEXT PRIMARY KEY,
  trade_group TEXT NOT NULL,
  updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 解析失敗的出工回報：寫法特殊、抓不出廠商或日期。留原文供人工檢查，
-- 之後解析規則改進、該則解析成功時會自動從這裡移除。
CREATE TABLE IF NOT EXISTS worklog_rejects (
  message_id  TEXT PRIMARY KEY,
  reported_at TIMESTAMP,
  reporter    TEXT,
  raw         TEXT,
  updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);
