# 部署指南

整套系統跑在 Netlify 上：工地人員用手機開一個網址就能填報、簽名、產生 PDF。

---

## 架構

```
            手機 / 電腦
                 │
                 ▼
   ┌──────────────────────────────────────┐
   │  Netlify                              │
   │                                       │
   │  dist/            前端靜態檔           │
   │  backend/cloud/functions/api.mts   所有 API  │
   │  Netlify Database (Postgres) 資料      │
   │  Netlify Blobs    照片／簽名／PDF       │
   └──────────────────────────────────────┘
```

Netlify Functions 只支援 Node.js / TypeScript，因此後端以 TypeScript 撰寫。
`backend/onprem/` 底下的 Python 版保留作為**公司內網（SQL Server）的部署目標**，
兩者共用同一份資料模型與 API 契約。

> ⚠ 兩份實作要同步維護。改動 API 行為時兩邊都要改，否則會分岔。

---

## 首次部署

1. Netlify 建立站台，**Project configuration → Build & deploy → Link repository**
   選這個 GitHub repo。建置設定會自動從 `netlify.toml` 讀取：
   - Build command：`python3 tools/build_frontend.py`
   - Publish directory：`dist`
   - Functions directory：`netlify/functions`
2. 設定環境變數（下一節）
3. **Trigger deploy**

部署時 Netlify 會依序做：`npm install` → 執行建置指令 → 套用
`netlify/database/migrations/` 下的 SQL → 發布。

資料庫與 Blobs 都會自動佈建，不需要手動建立或設定連線字串。

---

## 環境變數

**Site configuration → Environment variables**

| 變數 | 說明 |
|---|---|
| `SECRET_KEY` | **必填**。session cookie 簽章金鑰，請自行產生長隨機字串。未設定會使用程式碼中的開發預設值，等於任何人都能偽造登入 cookie |
| `INGEST_TOKENS` | **必填**。設備廠商推送權杖，格式 `vendor-a:xxx,vendor-b:yyy`。未設定會使用 repo 中公開的示範權杖 |
| `PUBLIC_DASHBOARD` | 預設 `false`（需登入）。設為 `true` 時任何拿到網址的人都能看到全公司缺失、廠商與工地資料，**僅限公司內網的戰情室大螢幕** |
| `HTTPS_ONLY` | 設 `true`，讓 session cookie 只走加密連線 |
| `BRAND_NAME` | 公司全名。不設定則顯示中性的預設名稱 |
| `BRAND_SHORT_NAME` | 公司簡稱 |

### ⚠ 改完環境變數一定要重新部署

Netlify Functions 讀的是**部署當下的環境變數快照**。
在後台改了變數而沒有重新部署，函式仍會使用舊值（或程式碼中的預設值），
而且不會有任何錯誤訊息，只會出現「權杖明明設對了卻一直 401」這種現象。

改完變數後務必 **Deploys → Trigger deploy → Deploy site**。

---

## 驗證清單

部署完成後逐項確認：

- [ ] `/api/health` 回傳 `ok: true`，且 `form_templates` 為 28、`form_items` 為 541
- [ ] 登入頁樣式正常（`/frontend/index.html`）
- [ ] 用測試帳號能登入
- [ ] 填一張自主檢查表 → 簽名 → 送出 → **PDF 能開啟且中文正常**
- [ ] 拍照上傳成功，缺失清單裡點「前／後」能看到照片
- [ ] 儀表板數字正確
- [ ] 用**設定的**廠商權杖推送設備資料回 200，用示範權杖回 401
      （若示範權杖仍可用，代表環境變數尚未生效，需重新部署）

---

## 上線前必做

| 項目 | 說明 |
|---|---|
| **改掉所有預設密碼** | `admin1234` 等是 migration 灌進去的，網址發給工地就等於公開 |
| **設定 `SECRET_KEY`** | 見上方說明 |
| **`INGEST_TOKENS` 換成真權杖** | 預設值公開在 repo 裡 |
| **`PUBLIC_DASHBOARD` 維持 `false`** | 見上方說明 |
| **確認示範資料是否要清掉** | migration 只灌表單模板與帳號，不含示範缺失。若曾手動灌過測試資料，上線前請清除 |

---

## 個資與公開性

這個網址上是**真實的工地缺失、真實的協力商名稱、真實的現場人員姓名與手寫簽名**。
與 repo 的去識別化不同，系統裡的資料本來就該是真實的——
因此保護方式是**存取控制**而不是去識別化：

- 填報與查詢都需要登入
- 儀表板預設也需要登入
- 照片、簽名與 PDF 都經 `/api/file/*` 供應，未登入無法取得
- 網址不要公開張貼；發給工地時用私訊或內部公告
- 離職或換人時停用帳號（`users.active` 設為 `false`）

---

## 遷移到公司內網

`backend/onprem/`（Python + SQL Server）就是內網版本，見 [README.md](README.md) 的資料庫章節。
兩邊資料表結構一致，資料可以直接匯出匯入：

- 雲端：Postgres（Netlify Database）
- 內網：SQL Server

`netlify/database/migrations/001_init/migration.sql` 與 `backend/onprem/app/db.py` 是同一套 schema
的兩種方言，欄位與語意完全對應。

---

## 常見問題

**PDF 中文變成方框？**
確認 `netlify.toml` 的 `[functions] included_files = ["backend/cloud/assets/fonts/**"]` 存在，
且 `backend/cloud/assets/fonts/NotoSansTC-Regular.otf` 在 repo 中。字型未打包進函式時，
`/api/inspections/{id}/pdf` 會直接報錯而不是產生方框。

**網頁英文字型跟品牌規範不一樣？**
`frontend/fonts/` 裡還沒放字型檔，瀏覽器回退到系統字體。
放入後即恢復，版面與色彩不受影響。詳見該資料夾的 README。
與 PDF 字型無關，兩者是獨立的。

**Migration 改了但沒生效？**
Netlify 只會套用尚未執行過的 migration。已套用過的檔案再修改不會重跑，
需要新增一個編號更大的 migration 目錄。
