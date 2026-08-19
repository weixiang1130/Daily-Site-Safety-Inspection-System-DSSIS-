-- 檢查人員姓名。
--
-- 現場目前共用同一組帳號登入，因此 inspector_id 只能代表「哪個工地帳號送的」，
-- 無法回答「這張表是誰檢查的」。稽核與事故調查都需要知道實際檢查人，
-- 所以由填報者自行填寫姓名。
--
-- 允許為空：既有資料沒有這個欄位，補值只會是猜測。

ALTER TABLE inspections ADD COLUMN IF NOT EXISTS inspector_name TEXT;
ALTER TABLE coordinations ADD COLUMN IF NOT EXISTS recorder_name TEXT;
