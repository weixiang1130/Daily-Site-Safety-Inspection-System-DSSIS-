-- 工地看板設定。
--
-- 緊急連絡人、每日作業循環時段、公告與無災害起算，由工地在看板管理頁
-- 自行維護。存成一份 JSON（board_config）而不是正規化資料表：整份設定
-- 一起讀一起寫、筆數有上限（驗證見 lib/site-board.ts，與地端
-- app/site_board.py 維持同一契約），拆表只會多出一堆兩三筆的小表。
--
-- board_revision 是防互蓋的版本鎖：儲存時必須帶上讀到的版本號，
-- 版本不符回 409 要求重新載入——兩個人同時編輯不會靜默互相蓋掉。
-- 雲端為唯一可寫端（已連線的檢視器在地端唯讀），隨表單同步帶回地端。

ALTER TABLE sites ADD COLUMN IF NOT EXISTS board_config TEXT;
ALTER TABLE sites ADD COLUMN IF NOT EXISTS board_revision INTEGER DEFAULT 0;
