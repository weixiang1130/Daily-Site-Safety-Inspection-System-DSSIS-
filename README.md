# 職安填報系統 + 戰情儀表板

把現行紙本的「每日協議、巡視及處理紀錄表」與 28 張「自主檢查表」搬到手機網頁上填寫，
現場填完即時進資料庫、即時上戰情儀表板，並自動產生 PDF 存查。
設備廠商依本系統定義的 API 格式推資料進來，資料庫留在公司自己手上，
不被任何單一廠商綁定。

---

## 目錄結構

```
safety-ops/
├─ frontend/          前端　純靜態網頁，無框架、無 CDN
│  ├─ index.html      登入
│  ├─ home.html       首頁
│  ├─ fill.html       自主檢查表填報（28 種）
│  ├─ coord.html      每日協議、巡視及處理紀錄表
│  ├─ findings.html   缺失清單／複驗
│  ├─ dashboard.html  戰情室大螢幕
│  ├─ admin.html      系統管理（工地／廠商／密碼）
│  ├─ brand.css       設計權杖（色階、字級、間距、陰影、動態）
│  ├─ style.css       元件樣式，只消費權杖
│  ├─ common.js       共用工具（品牌、簽名板、工地選單）
│  ├─ assets/         建築線條框景 SVG
│  └─ fonts/          網頁字型（自行放入，見該資料夾 README）
│
├─ backend/           後端
│  ├─ cloud/          雲端版　Netlify Functions（TypeScript）
│  │  ├─ functions/api.mts   單一函式處理所有 /api/*
│  │  ├─ lib/auth.ts         密碼驗證與 session
│  │  ├─ lib/pdf.ts          PDF 產出
│  │  └─ assets/fonts/       PDF 用中文字型（Noto Sans TC, OFL）
│  └─ onprem/         內網版　FastAPI + SQL Server
│     ├─ app/                與雲端版共用同一套資料模型與 API 契約
│     ├─ requirements*.txt
│     └─ Dockerfile
│
├─ netlify/database/migrations/   資料表定義與種子資料
│      ※ Netlify 規定的固定路徑，因此未併入 backend/
│
├─ data/             表單定義（forms.json）與本機主檔（*.local.json，不進版控）
├─ tools/            建置、匯入與維運腳本
├─ docs/             文件
│  ├─ README.md            系統說明（本檔的完整版）
│  ├─ DEPLOY.md            部署指南
│  ├─ API_SPEC.md          給設備廠商的串接規格
│  └─ DEIDENTIFICATION.md  去識別化規範與變更紀錄
│
├─ .githooks/pre-commit    commit 前自動執行去識別化掃描
├─ netlify.toml            Netlify 建置與函式設定
└─ package.json            雲端版相依套件
```

**為什麼有兩份後端**：雲端試辦跑在 Netlify（Functions 只支援 TypeScript），
公司內網最終要跑在 SQL Server 上（既有環境）。兩者共用同一套資料模型與 API 契約，
改動 API 行為時兩邊都要改。

---

## 快速開始

| 目的 | 指令 |
|---|---|
| 部署到雲端 | 見 [docs/DEPLOY.md](docs/DEPLOY.md) |
| 本機跑內網版 | `pip install -r backend/onprem/requirements-mssql.txt`　`python tools/create_db.py`　`python -m app.seed --demo`（於 `backend/onprem/` 下）|
| 建置前端 | `python tools/build_frontend.py` |
| 重新產生表單定義 | `python tools/extract_forms.py <docx>` |
| 重新產生 migration | `python tools/gen_migrations.py` |
| 匯入工地主檔 | `BASE=<網址> python tools/import_sites.py` |
| 端到端測試 | `python tools/e2e_test.py` |

---

## 推送前必讀

本 repo 為**公開**。任何進版控的內容都不得含真實人名、廠商名、工地名。
真實資料只存在於部署後的站台與本機的 `*.local.json`。

```bash
git config core.hooksPath .githooks     # clone 後執行一次
python tools/deidentify.py --check
```

完整規範見 **[docs/DEIDENTIFICATION.md](docs/DEIDENTIFICATION.md)**。

詳細說明請看 **[docs/README.md](docs/README.md)**。
