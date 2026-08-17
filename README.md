# 職安填報系統 + 戰情儀表板（原型）

把現行紙本的「每日協議、巡視及處理紀錄表」與 28 張「自主檢查表」搬到手機網頁上填寫，
現場填完即時進資料庫、即時上戰情儀表板，並自動產生 PDF 存查。
設備廠商（設備商甲／設備商乙／設備商丙…）未來依本系統定義的 API 格式推資料進來，
資料庫留在公司自己手上，不被任何單一廠商綁定。

---

## 快速啟動

```bash
pip install -r requirements.txt
python tools/create_db.py       # 在 SQL Server LocalDB 建立 SafetyOps 資料庫
python -m app.seed --demo       # 匯入 28 張表模板 + 示範資料
uvicorn app.main:app --host 0.0.0.0 --port 8010
```

沒有 SQL Server 的環境（例如 Linux 容器快速試跑），設 `DB_BACKEND=sqlite` 即可改用 SQLite。

瀏覽器開 <http://localhost:8010>

### 預設帳號

| 帳號 | 密碼 | 角色 |
|---|---|---|
| admin | admin1234 | 系統管理員 |
| pm01 | pm1234 | 工程專案主管 |
| safe01 | safe1234 | 職安人員（可複驗結案） |
| eng01 | eng1234 | 主辦工程師 |
| insp01 / insp02 / insp03 | insp1234 | 檢查人員 |

> 正式上線前務必更換所有預設密碼，並設定環境變數 `SECRET_KEY`。

---

## 頁面

| 路徑 | 用途 | 對象 |
|---|---|---|
| `/static/index.html` | 登入 | 全部 |
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
| 人員在場數、環境監測、CCTV 違規辨識 | **待設備廠商串接**（見 `API_SPEC.md`） |

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

> 身分驗證要換成公司 AD／SSO 時，只需改寫 `app/auth.py` 的 `authenticate()` 與
> `current_user()`，其餘 API 不受影響。

---

## 視覺設計

介面依企業品牌規範建立：**溫暖中性色（米／灰）+ 深石板藍點綴、無漸層、
銳利轉角（0–4px）、暖色調陰影、平緩動態（200ms，無彈跳）、
幾何標記優先於圖示、不使用 emoji**。

| 檔案 | 內容 |
|---|---|
| `static/brand.css` | 設計權杖：色階、字級、間距、圓角、陰影、動態；含 `@font-face` |
| `static/style.css` | 元件樣式，**只消費權杖，不硬寫色碼** |
| `static/fonts/` | 自行托管的字型檔（見該資料夾 README） |

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
