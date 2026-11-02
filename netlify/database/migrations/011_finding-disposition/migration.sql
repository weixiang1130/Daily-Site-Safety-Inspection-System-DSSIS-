-- 缺失處置可複選＋覆驗紀錄（2026-10-30 工地看板檢討會）
--
-- 處置：一筆缺失可能同時「立即改善＋扣款」或「限期改善＋代工＋扣款」，原本的
-- action_type 只能二選一（onsite / scheduled）。改存逗號分隔的處置清單：
--   onsite=立即改善  scheduled=限期改善  proxy=代工  deduct=扣款
-- action_type 保留且由處置推得（有限期或代工＝scheduled），逾期判斷、地端同步、
-- 舊報表都照舊讀它。扣款金額寫既有的 penalty 欄（原本沒有任何程式寫入）。
--
-- 覆驗：照片上傳不等於改善通過。改善回報→待複驗（fixed）→安衛人員覆驗才結案；
-- 覆驗不通過退回改善中，原因與前次改善照片路徑累加記在 review_note，不刪任何紀錄。

ALTER TABLE findings ADD COLUMN IF NOT EXISTS dispositions TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS review_note TEXT;

UPDATE findings SET dispositions = action_type WHERE dispositions IS NULL;
