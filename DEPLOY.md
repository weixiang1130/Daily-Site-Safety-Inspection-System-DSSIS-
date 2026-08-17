# 部署指南

目標：讓工地人員用手機開一個網址就能填報、簽名、產生 PDF，
同時收集使用回饋，之後再整套遷回公司內網。

---

## 架構

```
                手機 / 電腦
                     │
                     ▼
        ┌────────────────────────┐
        │  Netlify（前端靜態檔）   │   ← 使用者只看到這個網址
        │  /api/*  → 反向代理 ────┼──┐
        │  /uploads/* → 反向代理 ─┼──┤
        └────────────────────────┘  │
                                    ▼
                    ┌──────────────────────────┐
                    │  Python 後端（FastAPI）    │
                    │  + 資料庫                  │
                    │  + 持久化磁碟（照片／PDF）  │
                    └──────────────────────────┘
```

**為什麼後端不能放 Netlify**：Netlify Functions 只支援 Node.js / TypeScript，
不支援 Python。要把後端搬上去必須把 FastAPI 整個重寫成 TypeScript，
資料庫也得從 SQL Server 換成 Postgres——而本專案最終要遷回公司內網的
SQL Server，那份工會白做。

**反向代理讓這件事不成問題**：瀏覽器只跟 Netlify 溝通，
因此是同源請求，cookie 正常運作、不需要處理 CORS，
使用者也完全感覺不到後端在別的地方。

**遷移到內網時**：把 `BACKEND_ORIGIN` 改成內網主機位址即可，程式碼一行都不用改。
或直接由內網主機同時供應前端與 API，連 Netlify 都不需要。

---

## 步驟一：部署後端

### 選項 A：Render（試辦推薦）

不需信用卡即可起步、支援持久化磁碟、直接接 GitHub 自動部署、
有新加坡機房（離台灣最近）。

1. 到 <https://render.com> 用 GitHub 帳號登入
2. **New → Blueprint**，選這個 repo，Render 會讀取 `render.yaml`
   自動建立 Web Service 與 PostgreSQL 資料庫
3. 部署完成後，到該服務的 **Environment** 補上不進版控的變數：

   | 變數 | 值 |
   |---|---|
   | `BRAND_NAME` | 公司全名 |
   | `BRAND_SHORT_NAME` | 公司簡稱 |
   | `INGEST_TOKENS` | `vendor-a:<權杖>,vendor-b:<權杖>` |

4. 開 **Shell** 執行一次初始化：

   ```bash
   python -m app.seed
   ```

   > 加 `--demo` 會另外塞入示範資料。**正式收集現場資料時不要加**，
   > 否則儀表板會混入假資料。

5. 記下服務網址，例如 `https://safety-ops-api.onrender.com`
6. 開 `https://<網址>/api/health` 確認回傳 `"ok": true` 與資料庫資訊

> ⚠ 免費方案沒有持久化磁碟且閒置 15 分鐘會休眠（照片與 PDF 會遺失、
> 工地開啟要等三十秒喚醒）。**實際給工地使用請用 Starter 以上方案。**

### 選項 B：Azure App Service（若要與公司內網同生態）

公司若已是 Microsoft 環境（AD、SQL Server、Microsoft 365），
選 Azure 的好處是**資料庫可以用 Azure SQL Database，
與內網 SQL Server 是同一種資料庫**，內網遷移時 DDL 與查詢完全不需重驗。

1. 建立 Azure SQL Database，取得連線字串
2. 建立 App Service（Linux, Python 3.11 或容器）
3. 應用程式設定加入：

   ```
   DATABASE_URL=mssql+pyodbc://user:pw@srv.database.windows.net/SafetyOps?driver=ODBC+Driver+18+for+SQL+Server
   SECRET_KEY=<隨機字串>
   HTTPS_ONLY=true
   PUBLIC_DASHBOARD=false
   BRAND_NAME=...
   ```

4. 啟動命令：`uvicorn app.main:app --host 0.0.0.0 --port 8000`
5. 掛載 Azure Files 到 `/app/uploads` 供照片與 PDF 存放

### 選項 C：容器（任何平台，含公司內網）

```bash
docker build -t safety-ops .
docker run -d -p 8000:8000 \
  -e DATABASE_URL="..." \
  -e SECRET_KEY="..." \
  -e HTTPS_ONLY=true \
  -v safety-uploads:/app/uploads \
  safety-ops
```

---

## 步驟二：部署前端到 Netlify

1. <https://app.netlify.com> → **Add new site → Import an existing project**
2. 選這個 GitHub repo。Netlify 會讀取 `netlify.toml`：
   - Build command：`python3 tools/build_frontend.py`
   - Publish directory：`dist`
3. **Site configuration → Environment variables** 新增：

   | 變數 | 值 |
   |---|---|
   | `BACKEND_ORIGIN` | 步驟一取得的後端網址（結尾不要加斜線） |

4. **Deploys → Trigger deploy**（環境變數要重新部署才會生效）
5. 開 Netlify 給的網址，應該直接進到登入頁

### 驗證清單

- [ ] 登入頁能開啟，樣式與字型正常
- [ ] 用 `insp03 / insp1234` 能登入
- [ ] 填一張自主檢查表 → 簽名 → 送出 → **PDF 能開啟且中文正常**
- [ ] 拍照上傳能成功，缺失清單裡點「前／後」能看到照片
- [ ] 儀表板數字正確
- [ ] **重新部署後端後，之前上傳的照片仍在**（確認持久化磁碟有生效）

---

## 上線前必做

| 項目 | 說明 |
|---|---|
| **改掉所有預設密碼** | `admin1234` 等預設密碼絕不可留在對外網站上 |
| **設定 `SECRET_KEY`** | 未設定會使用程式碼中的開發用預設值，等於沒有保護 |
| **`PUBLIC_DASHBOARD` 維持 `false`** | 設為 `true` 時任何拿到網址的人都能看到全公司缺失、廠商與工地資料。**只有在公司內網的戰情室大螢幕才可以開啟** |
| **`HTTPS_ONLY=true`** | 讓 session cookie 只走加密連線 |
| **確認持久化磁碟** | 沒掛磁碟的話，每次重新部署照片與 PDF 全部消失 |
| **`INGEST_TOKENS` 換成真權杖** | 預設值是公開在 repo 裡的示範權杖 |

---

## 個資與公開性提醒

這個網址一旦公開，上面就是**真實的工地缺失、真實的協力商名稱、
真實的現場人員姓名與手寫簽名**。與 repo 的去識別化不同，
系統裡的資料本來就該是真實的——因此保護方式是**存取控制**而不是去識別化：

- 填報與查詢都需要登入（已實作）
- 儀表板預設也需要登入（`PUBLIC_DASHBOARD=false`）
- 網址不要公開張貼；發給工地時用私訊或內部公告
- 離職或換人時記得停用帳號（`users.active` 設為 false）

---

## 常見問題

**PDF 中文變成方框？**
不會。PDF 使用 reportlab 內建的 Adobe 繁體中文 CID 字型 `MSung-Light`，
容器內不需安裝任何字型檔。網頁字型才需要 `static/fonts/`，兩者無關。

**網頁英文字型跟品牌規範不一樣？**
`static/fonts/` 裡還沒放字型檔，瀏覽器回退到系統字體。
放入字型檔後即恢復，版面與色彩不受影響。詳見該資料夾的 README。

**後端換位置了？**
只改 Netlify 的 `BACKEND_ORIGIN` 環境變數並重新部署，前端程式碼不用動。

**Netlify 部署失敗說找不到 python3？**
確認 `netlify.toml` 的 `PYTHON_VERSION = "3.11"` 存在。
建置腳本只用到標準函式庫，不需安裝任何套件。
