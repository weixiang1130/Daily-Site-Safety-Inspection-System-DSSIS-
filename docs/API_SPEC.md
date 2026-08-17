# 職安戰情室 — 設備資料串接規格（給設備廠商）

本文件說明各設備廠商（門禁人臉辨識、環境監測、CCTV 影像辨識等）
如何將資料送入本公司職安戰情室系統。

**核心原則：資料格式由本公司定義，各廠商依此格式發送。**
未來新增設備或更換儀表板廠商時，只需多串一支 API，不需更換整套設備。

---

## 1. 串接方式

廠商端主動推送（Push）到本公司端點。

```
POST  https://<本公司主機>/api/v1/ingest/device
Content-Type: application/json
X-Vendor-Token: <本公司核發給貴司的權杖>
```

推送頻率建議：

| 資料類型 | 建議頻率 | 說明 |
|---|---|---|
| 門禁進出 | **逐筆即時**（或至少每 5 分鐘批次） | 目前僅每日 3 個時段的總人數，無法支撐即時戰情 |
| 環境監測 | 每 5～15 分鐘 | |
| 影像辨識告警 | 即時 | 觸發時立即送出 |

---

## 2. 請求格式

```json
{
  "vendor_code": "vendor-a",
  "site_code": "SITE-A",
  "device_type": "access",
  "device_id": "GATE-01",
  "readings": [
    { "metric": "headcount_in",  "value_num": 128, "reading_at": "2026-08-17T09:00:00" },
    { "metric": "headcount_out", "value_num": 12,  "reading_at": "2026-08-17T09:00:00" },
    { "metric": "alarm", "value_text": "未戴安全帽", "reading_at": "2026-08-17T09:03:12" }
  ]
}
```

### 欄位定義

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `vendor_code` | string | ✔ | 廠商代碼，由本公司核發 |
| `site_code` | string | ✔ | 工地代碼，由本公司提供對照表 |
| `device_type` | string | ✔ | `access`＝門禁／`env`＝環境監測／`cctv_ai`＝影像辨識 |
| `device_id` | string | ✔ | 設備編號（閘門、感測器、攝影機） |
| `readings[].metric` | string | ✔ | 量測項目，見下表 |
| `readings[].value_num` | number | △ | 數值型資料 |
| `readings[].value_text` | string | △ | 文字型資料（告警內容等） |
| `readings[].reading_at` | ISO8601 | ✔ | **資料發生時間**，非送出時間 |

`value_num` 與 `value_text` 至少擇一。

### metric 對照表

| device_type | metric | 說明 |
|---|---|---|
| `access` | `headcount_in` / `headcount_out` | 進場／出場人次 |
| `access` | `headcount_present` | 目前在場人數 |
| `access` | `entry` | **單筆進出紀錄**（見下節，最重要） |
| `env` | `pm25` / `pm10` / `noise` / `temperature` / `humidity` / `wbgt` | 環境監測值 |
| `cctv_ai` | `alarm` | 違規辨識告警（`value_text` 填違規類型） |

---

## 3. 逐筆進出紀錄（最重要）

**目前的問題**：現行僅提供每日 3 個時段的「總人數」。
戰情室要做「即時在場人數、特定證照人員是否在場、異常滯留」等指標，
必須要有**逐筆原始進出紀錄**。

```json
{
  "vendor_code": "vendor-a",
  "site_code": "SITE-A",
  "device_type": "access",
  "device_id": "GATE-01",
  "readings": [
    {
      "metric": "entry",
      "reading_at": "2026-08-17T07:32:11",
      "value_text": "in",
      "person": {
        "employee_no": "EMP-081",
        "name": "張小明",
        "vendor_name": "甲營造",
        "trade": "鋼筋",
        "certs": ["急救人員", "施工架作業主管"]
      }
    }
  ]
}
```

> 若貴司系統本身未儲存原始進出紀錄，請於下次會議明確告知，
> 我方需據此評估是否調整設備或改由其他來源取得。

---

## 4. 回應

成功：

```json
{ "ok": true, "accepted": 3 }
```

失敗：

| HTTP | 意義 | 處理 |
|---|---|---|
| 401 | 權杖驗證失敗 | 檢查 `X-Vendor-Token` 與 `vendor_code` 是否相符 |
| 400 | 格式錯誤 | 依 `detail` 訊息修正 |
| 5xx | 本公司端異常 | 請保留資料並重送（建議指數退避，最多重試 3 次） |

**重送規則**：同一 `device_id` + `metric` + `reading_at` 重複送出時，
我方會保留全部原始紀錄，不會造成統計重複計算，廠商端可安心重送。

---

## 5. 查詢 API（本公司或儀表板廠商使用）

```
GET /api/v1/device/latest?site_code=SITE-A&device_type=access&limit=50
```

```
GET /api/dashboard?days=30[&site_id=<id>]
```

`/api/dashboard` 回傳戰情室所需的全部彙總資料（KPI、災害類別分布、
廠商缺失排行、逾期清單、趨勢、各工地紅黃綠燈）。
**儀表板廠商只需讀這一支 API 即可，不需要接觸原始資料庫。**

---

## 6. 給廠商的確認事項（下次會議請逐項回覆）

1. 貴司設備是否具備**對外主動發送 API** 的能力？若無，需要多少工時／費用開發？
2. 貴司系統是否**儲存原始逐筆進出紀錄**？可回溯多久？
3. 現場設備是否有**對外網路**？若為封閉系統，需要何種架構才能送出資料
   （閘道器？定時匯出？）
4. 是否可**改由本公司格式**發送（而非要求我方配合貴司既有格式）？
   若貴司已有現成 API，請提供文件，我方評估由儀表板端配合調整。
5. 影像（CCTV）串流是否可提供 **RTSP／HLS 位址**供戰情室牆面調用？
   AI 違規辨識是在端點做還是回總部做？
6. 上述各項的**報價**：一次性開發費、年費、新增設備時的追加費用。

---

## 7. 對本公司採購的要求

未來新進設備廠商，合約中應載明：

> 廠商所提供之設備／系統，須能依業主指定之 API 格式，
> 主動發送原始資料至業主指定端點，且不得就此另行收取資料介接費用。

