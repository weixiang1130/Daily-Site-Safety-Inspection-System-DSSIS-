-- 棟別。
--
-- 主場站雖領有兩張建照，實際上是同一塊工地、同一批人在管，
-- 因此填報不分成兩個工地，改以「棟別」欄位（如商辦棟／住宅棟）區分。
-- 缺失沿用所屬表單的棟別，讓儀表板與清單能顯示缺失發生在哪一棟；
-- 彙總統計則不分棟、不分工地，一律以填報資料整體計算。
--
-- 允許為空：既有資料沒有這個欄位，補值只會是猜測。

ALTER TABLE inspections   ADD COLUMN IF NOT EXISTS building TEXT;
ALTER TABLE coordinations ADD COLUMN IF NOT EXISTS building TEXT;
ALTER TABLE findings      ADD COLUMN IF NOT EXISTS building TEXT;
