# 職安填報系統 + 戰情儀表板（原型）

把現行紙本的「每日協議、巡視及處理紀錄表」與 28 張「自主檢查表」搬到手機網頁上填寫，
現場填完即時進資料庫、即時上戰情儀表板，並自動產生 PDF 存查。
設備廠商（設備商甲／設備商乙／設備商丙…）未來依本系統定義的 API 格式推資料進來，
資料庫留在公司自己手上，不被任何單一廠商綁定。

---

## 快速啟動

```bash
pip install -r backend/onprem/requirements-mssql.txt
python tools/create_db.py       # 在 SQL Server LocalDB 建立 SafetyOps 資料庫
python -m app.seed --demo       # 匯入 28 張表模板 + 示範資料
uvicorn app.main:app --host 0.0.0.0 --port 8010
```

資料庫驅動依環境分開安裝，因為不同環境用不同資料庫：

| 環境 | 安裝 | 說明 |
|---|---|---|
| 本機開發 / 公司內網 | `backend/onprem/requirements-mssql.txt` | SQL Server，需先裝 Microsoft ODBC Driver |
| 雲端試辦 | `backend/onprem/requirements-postgres.txt` | PostgreSQL |
| 快速試跑 | `requirements.txt` + `DB_BACKEND=sqlite` | 免驅動 |

**部署到雲端請看 [DEPLOY.md](DEPLOY.md)**（Netlify 前端 + Python 後端的架構與步驟）。

瀏覽器開 <http://localhost:8010>

### 預設帳號

初始帳號由 `netlify/database/migrations/003_seed-master` 建立：

| 帳號 | 角色 |
|---|---|
| admin | 系統管理員 |
| pm01 | 工程專案主管 |
| safe01 | 職安人員（可複驗結案） |
| eng01 | 主辦工程師 |
| insp01 / insp02 / insp03 | 檢查人員 |

初始密碼為 `<帳號前綴>1234` 形式，僅供本機開發使用。

> **任何對外環境部署後的第一件事，就是用管理頁（`/static/admin.html`）
> 逐一更換密碼。** 初始密碼寫在公開的 migration 裡，等同沒有保護。
> 更換後請一併確認登入頁與文件沒有殘留舊密碼。

---

## 頁面

| 路徑 | 用途 | 對象 |
|---|---|---|
| `/frontend/index.html` | 登入 | 全部 |
| `/static/home.html` | 首頁（今日填報、近期單據） | 現場 |
| `/static/fill.html` | 自主檢查表填報（28 種） | 現場 |
| `/static/coord.html` | 每日協議、巡視及處理紀錄表 | 現場 |
| `/static/findings.html` | 缺失清單／複驗結案 | 職安、主管 |
| `/static/dashboard.html` | **戰情室大螢幕**（免登入、每分鐘自動刷新） | 戰情室 |

### 填報設計重點

不是把紙本表格原樣搬上網，而是問卷式流程：

```
選工地 → 選今日作業類別 → 只帶出該表單的檢查項目
→ 逐項「合格／不合格／不適用」大按鈕（可一鍵全部合格）
→ 只有「不合格」才展開：災害類別、責任廠商、責任人、
   當場改善 or 限期改善（+期限）、改善說明、前後照片
→ 手寫簽名 → 送出 → 自動產生 PDF
```

全部合格的情況下，一張 26 項的表約 30 秒可填完。

---

## 資料模型

| 資料表 | 說明 |
|---|---|
| `sites` / `vendors` / `users` | 工地、責任廠商、使用者 |
| `form_templates` / `form_items` | 28 張檢查表模板，共 541 個檢查項目 |
| `inspections` / `inspection_results` | 巡檢單與逐項結果 |
| `coordinations` / `coordination_attendees` | 每日協議巡視表與參加人員 |
| **`findings`** | **缺失單 —— 儀表板的核心資料** |
| `signatures` | 電子簽名（簽名圖、簽署人、時間、IP） |
| `device_readings` | 設備廠商 API 推送的原始資料落地表 |

### 廠商由現場自行填列

協力商在各工地差異很大、且會隨工程階段更換，不可能由管理員預先維護齊全。
因此填報畫面的「責任廠商」是**可輸入的欄位**（附既有廠商的建議清單），
現場可以直接打新廠商名稱。

送出時前端會呼叫 `POST /api/vendors/resolve`，該名稱若尚未建檔就自動建立
正式的廠商資料。**這一步不能省略成自由文字**，否則儀表板的
「廠商缺失排行」會漏掉現場臨時新增的廠商。

名稱比對時忽略大小寫與前後空白，避免同一家廠商被建成多筆。
若真的建重複了，可到管理頁改名或停用。

### 兩個關鍵設計

1. **`hazard_code` 災害類別代碼化**
   直接沿用 28 張檢查表原本的分類軸（墜落／感電／倒塌崩塌／物體飛落／衝撞／
   被夾被捲／穿刺／火災／局限空間／危險機械吊掛／門禁與防護具／環境整潔／
   一般管理／其他）。缺失一律先選大類，禁止純自由文字 —— 這是能不能做統計的分水嶺。

2. **`action_type` 區分「當場改善」與「限期改善」**
   現行紙本幾乎全部勾「立即改善完成」，若照搬進儀表板，改善率永遠 100%、
   毫無管理價值。當場改善開單即結案但仍計入缺失次數；
   只有限期改善才進逾期追蹤，`overdue` 紅燈才有意義。

---

## 儀表板指標

| 指標 | 來源 |
|---|---|
| 今日新增缺失 / 未結案 / **逾期未改善** | `findings` |
| 當場改善 vs 限期改善件數、結案率、改善時效中位數 | `findings` |
| 災害類別分布、廠商缺失排行 | `findings` × `hazard_code` / `vendor` |
| 近 14 日缺失趨勢 | `findings.found_at` |
| 各工地紅黃綠燈 | 有逾期→紅、有未結案→黃、其餘→綠 |
| 今日已交巡檢單 | `inspections` |
| 工地環境（熱指數、噪音、PM2.5／PM10）與危害等級 | 微型氣象站，每 15 分鐘自動抓取 |
| 現場在場人數、今日進出場人次 | 門禁／人臉辨識看板服務，每 10 分鐘自動抓取 |
| 現場監視畫面 | NVR 快照，由後端代理（見下） |
| 最新填報表單與結果 | `inspections` + `coordinations` |
| CCTV 違規辨識（AI） | **待設備廠商串接**（見 `API_SPEC.md`） |

---

## 工地環境與危害分級

微型氣象站的數據由 `backend/cloud/functions/weather-poll.mts` 每 15 分鐘抓取一次，
實際邏輯在 `backend/cloud/lib/weather.ts`。只抓「已連線」的測站，斷線的略過。

### 危害等級為什麼自己算

廠商平台本身有「危害等級」欄位，但實測（2026-08-19）在熱指數 49.4°C 的情況下
仍回報 0。這比沒有數據更危險——工地看了會誤以為現場安全。

因此本系統一律以自己的邏輯分級，門檻集中在 `backend/cloud/lib/hazard.ts`：

| 指標 | 分界（注意／警戒／危險／極度危險） | 依據 |
|---|---|---|
| 熱指數 | 27 / 32 / 41 / 54 °C | 職安署《高氣溫戶外作業勞工熱危害預防指引》，與美國 NWS 熱指數分級相同 |
| 噪音 | 80 / 85 / 90 dB | 職業安全衛生設施規則第 300 條 |
| PM2.5 | 15.5 / 35.5 / 54.5 μg/m³ | 環境部空氣品質指標 AQI（24 小時值） |
| PM10 | 51 / 101 / 255 μg/m³ | 環境部空氣品質指標 AQI（24 小時值） |

測站整體等級取各指標中的**最高**者，不取平均——任何一項達到危險都必須反映出來，
不能被其他正常的指標稀釋掉。

廠商的原始值另存為 `vendor_hazard_level`，只作為與廠商釐清問題時的佐證，
不進入任何告警判斷。

### 兩個必須留意的限制

1. **噪音是即時音壓級，不是 8 小時日時量平均值。** 法規的 85／90 分貝是以
   工作日 8 小時日時量平均定義，因此牆上的顏色只能當作現場提醒，
   不可直接當成法規符合性的判定結果。
2. **PM2.5／PM10 的 AQI 分界原本以 24 小時平均值定義**，此處套用在即時濃度上，
   同樣只作為現場提醒。

溫濕度本身不單獨分級——熱指數已經同時涵蓋這兩者，且它才是能直接對應到
「加強休息、調整工時、停止作業」的指標。若廠商停供熱指數，只要溫濕度還在，
系統會以 NWS 迴歸式自行推算，不受制於對方。


### 人員進出人次

由 `backend/cloud/functions/headcount-poll.mts` 每 10 分鐘抓取一次，
邏輯在 `backend/cloud/lib/headcount.ts`。

**只取彙總數字（在場、今日進場、今日出場），不取進出明細。** 明細裡有姓名、
員工編號、所屬廠商與人臉辨識紀錄，屬於個人資料；戰情室要回答的是「現場有
多少人」而不是「是誰」，沒有存進來的資料就不會外洩，也不必為它另外做保護。

在場人數放在指標列的最前面——緊急應變時要先知道現場還有多少人。

> **待廠商處理**：該看板服務目前沒有任何存取控制，任何人知道網址就能取得
> 上述個資。已列入待與廠商釐清事項。


### 監視畫面為什麼要後端代理

不能直接把監視器網址嵌進戰情室，有三道限制：

1. 主機回應 `X-Frame-Options: SAMEORIGIN`，明確禁止被別的網站 iframe
2. 主機是 http，戰情室是 https，瀏覽器會擋掉混合內容
3. 快照需要 HTTP Digest 驗證，帳密不能放到前端

因此改由 `backend/cloud/lib/cctv.ts` 在伺服器端完成驗證、取回 JPEG，
再從本站的 https 網域送出（`/api/cctv/snapshot?channel=N`）。
帳密只存在環境變數，不會出現在瀏覽器。

Digest 規定用 MD5，而 Web Crypto 只有 SHA 系列，因此該檔內含一份 MD5
實作；它只用於驗證握手，未用於任何安全性用途，且已用 RFC 1321 的測試
向量驗證過。

只接受 `CCTV_CHANNELS` 列出的頻道，避免這支路由變成可任意存取內網主機的跳板。
畫面是定時更換的快照而非串流——真正的串流是 RTSP，瀏覽器原生播不了。

### 戰情室的兩個版面原則

1. **整頁不出現捲軸。** 牆上的畫面沒有人會去滑，看不到就等於沒有。
   清單一律自動換頁，每頁幾列由容器實際高度算出，換螢幕不必調參數。
2. **以主場站為主體。** 上方即時區塊（環境、人數、監視）只看
   `PRIMARY_SITE_CODE` 指定的工地；下方清單仍涵蓋所有工地，
   因為現場只有一個戰情室，別的工地交了什麼也得看得到。

視覺與填報系統共用同一套色彩、字體與元件，只有版面規則不同。

### 檢查人員

現場共用同一組帳號登入，`inspector_id` 只能代表「哪個工地帳號送的」，
無法回答「這張表是誰檢查的」。因此填報時必須填寫檢查人員姓名
（協議表為紀錄人員），PDF 與清單一律以此為準，未填才退回帳號名稱。

### PDF 中文字型

`backend/cloud/assets/fonts/NotoSansTC-Regular.ttf` 由 `tools/build_pdf_font.py`
產生：先把輪廓轉成 TrueType，再縮到系統需要的字。

**兩件事都不可以改**：

1. 不可以改用同目錄的 `.otf`
2. `pdf.ts` 的 `embedFont` 不可以開 `subset: true`

fontkit 的子集化對這支字型是壞的，會掉字——實測 21 個字裡只畫得出 3～13 個，
且不同格式掉的字還不一樣，正是先前 PDF 中文全空白的原因。完整實測數據見
`tools/build_pdf_font.py` 的說明。代價是 PDF 約 1.8 MB，換來的是字一定畫得出來。

字型收錄範圍是 ASCII、常用標點、系統本身的文字，加上 Big5 常用字（5401 字）。
範圍外的字不會靜默消失：`font-coverage.ts` 讓 `pdf.ts` 把它畫成 □ 並在記錄檔
留下警告，之後依實際資料把字補進收錄範圍再重建即可。

---

## 資料庫

**開發與正式環境使用同一種資料庫（SQL Server）**，因此 DDL、型別行為與 SQL 語法
在遷移時不需重新驗證。開發階段用 LocalDB，正式環境指向公司內網的 SQL Server。

- 所有中文欄位使用 `Unicode` / `UnicodeText` → `NVARCHAR` / `NVARCHAR(MAX)`，
  避免非 Unicode 定序造成中文亂碼
- 布林條件一律寫 `== True`，**不可用 `.is_(True)`** —— 後者在 SQL Server 會編譯成
  不合法的 `IS 1`（SQLite 反而正常，是典型的跨資料庫地雷）

### 索引

| 索引 | 服務的查詢 |
|---|---|
| `ix_findings_found_site` (found_at, site_id) | 儀表板主查詢：某區間＋某工地的缺失 |
| `ix_findings_status_due` (status, due_date) | 未結案／逾期追蹤 |
| `ix_findings_hazard` / `ix_findings_vendor` | 災害類別分布、廠商缺失排行 |
| `ix_inspections_date_site` | 今日巡檢達成率 |
| `ix_device_site_type_time` | 設備資料（未來量體最大的表） |

### 工具

```bash
python tools/create_db.py            # 建立資料庫（--drop 可重建）
python tools/inspect_db.py           # 檢視實際 DDL、筆數、中文抽樣驗證
curl http://localhost:8010/api/health # 確認目前連到哪個資料庫
```

---

## 部署

### 階段一：公有雲試辦（現況）

任何可跑 Python 的雲端主機／容器皆可：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8010 --workers 2
```

- `uploads/` 需掛載持久化磁碟（照片、簽名圖、產出的 PDF）
- PDF 中文字型使用 reportlab 內建的 CID 字型 `MSung-Light`，**不需安裝任何字型檔**，Linux 容器可直接跑
- 前端不依賴任何 CDN，內網離線環境也能用

### 階段二：搬回公司內網

只需改環境變數，程式碼不用動：

```bash
set MSSQL_SERVER=SRV-DB01
set MSSQL_DATABASE=SafetyOps
set SECRET_KEY=<請自行產生>
set INGEST_TOKENS=vendor-a:<權杖>,vendor-b:<權杖>,vendor-c:<權杖>
```

使用帳密而非 Windows 驗證時，直接給完整連線字串 `DATABASE_URL`。
完整設定項見 `.env.example`。

> 身分驗證要換成公司 AD／SSO 時，只需改寫 `backend/onprem/app/auth.py` 的 `authenticate()` 與
> `current_user()`，其餘 API 不受影響。

---

## 視覺設計

介面依企業品牌規範建立：**溫暖中性色（米／灰）+ 深石板藍點綴、無漸層、
銳利轉角（0–4px）、暖色調陰影、平緩動態（200ms，無彈跳）、
幾何標記優先於圖示、不使用 emoji**。

| 檔案 | 內容 |
|---|---|
| `frontend/brand.css` | 設計權杖：色階、字級、間距、圓角、陰影、動態；含 `@font-face` |
| `frontend/style.css` | 元件樣式，**只消費權杖，不硬寫色碼** |
| `frontend/fonts/` | 自行托管的字型檔（見該資料夾 README） |

- **換色只需改 `brand.css`**，元件樣式一行都不用動
- 深色情境（戰情室大螢幕）是同一組色階的深色端，用 `.theme-dark` 切換，
  不是另一套配色，因此品牌調性一致
- KPI 數字與章節編號使用等寬字並開啟 `tabular-nums`，是版面的視覺重心
- `▎` 幾何標記用於區段標題，取代圖示
- 右下角建築線條框景以 inline SVG 注入（`common.js`），繼承 `currentColor`
  以隨深淺色主題變化

### 公司名稱

因 repo 公開，**公司名稱不寫在程式碼裡**，由 `/api/branding` 從環境變數提供：

```bash
set BRAND_NAME=○○營造股份有限公司
set BRAND_SHORT_NAME=○○營造
```

未設定時顯示中性的預設名稱。設定後版頭、頁面標題即為完整品牌。

---

## 去識別化（推送前必讀）

本 repo 為**公開**，示範資料中的人名、廠商名、工地名**全部是虛構代稱**。
每次推送前必須執行去識別化掃描，`.githooks/pre-commit` 會自動把關。

```bash
git config core.hooksPath .githooks     # clone 後執行一次，啟用 hook
python tools/deidentify.py --check      # 手動掃描
```

完整規範見 **[DEIDENTIFICATION.md](DEIDENTIFICATION.md)**。

---

## 目錄

```
safety-ops/
├─ app/
│  ├─ main.py      FastAPI 路由（填報、缺失、儀表板、對外 API）
│  ├─ db.py        SQLAlchemy 資料模型
│  ├─ auth.py      帳密驗證（日後可換 AD／SSO）
│  ├─ pdf.py       PDF 產出（reportlab，內建中文 CID 字型）
│  └─ seed.py      初始化：匯入 28 張表模板、建立示範資料
├─ data/forms.json 28 張檢查表定義（由 tools/extract_forms.py 產生）
├─ static/         前端網頁（無框架、無 CDN）
├─ tools/
│  ├─ extract_forms.py   從自主檢查表 docx 範本重新抽取表單定義
│  ├─ create_db.py       建立 SQL Server 資料庫
│  ├─ inspect_db.py      檢視實際 DDL 與資料
│  └─ deidentify.py      去識別化掃描 / 置換 / 還原
├─ .githooks/pre-commit  commit 前自動執行去識別化掃描
├─ uploads/              照片、簽名圖、產出的 PDF（不進版控）
├─ API_SPEC.md           給設備廠商的 API 串接規格
└─ DEIDENTIFICATION.md   去識別化規範與變更紀錄
```

### 檢查表範本更新時

公司若修訂了自主檢查表 docx 範本，重跑：

```bash
python tools/extract_forms.py "<新版 docx 路徑>"
python -m app.seed
```

`seed` 是冪等的：同 `form_code` + `seq` 會更新而非重複新增，既有巡檢紀錄不受影響。

---

## 待辦（第二階段）

- [ ] 離線填報（Service Worker + IndexedDB 暫存，回到有訊號時自動上傳）
- [ ] 多人簽核鏈（目前一張單記錄一位簽署人，資料表已支援多筆）
- [ ] 月曆型表單（捲揚機、施工電梯、高空工作車、滅火器）的逐日填報介面
- [ ] 缺失照片牆輪播（戰情室大螢幕用）
- [ ] MCP Server：讓主管用自然語言查詢（等資料庫累積真實資料後再做）
- [ ] 人員／證照主檔與門禁系統對接，協議表出席人員自動帶入
