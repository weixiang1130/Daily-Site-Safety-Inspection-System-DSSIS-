# 去識別化規範

本專案的 GitHub repo 為**公開**。因此有一條不可妥協的規則：

> **任何進版控的內容，都不得含真實人名、真實廠商名、真實工地名或公司內部敏感字詞。**

程式碼、架構、資料模型公開沒有問題；問題只在資料。本文件說明如何確保每次推送都符合規範。

---

## 一、為什麼需要這件事

原型開發時為了讓儀表板「一開啟就有東西可看」，示範資料直接取自現場真實單據，
其中包含協力商員工的姓名、實際工地名稱與廠商名稱。這些一旦推上公開 repo，
即使事後刪除也可能已被搜尋引擎或第三方鏡像索引，實務上無法收回。

其中**真實人名屬第三人個資**，風險最高。

---

## 二、機制

```
deid_rules.local.json     真實 ↔ 代稱對照表（本機限定，.gitignore 排除）
deid_rules.example.json   結構範例（進版控，內容全為假資料）
tools/deidentify.py       掃描 / 置換 / 還原
.githooks/pre-commit      commit 前自動掃描，發現真實字詞即擋下
```

**關鍵設計：對照表本身就是還原金鑰，因此它不進版控。**
只把掃描工具放進 repo，任何拿到 repo 的人都無法從中還原真實名稱。

### 代稱規則

| 類型 | 真實內容 | 代稱形式 |
|---|---|---|
| 工地 | 各實際工程名稱 | 示範工地 A／B／C，代碼 `SITE-A`／`SITE-B`／`SITE-C` |
| 協力廠商 | 各實際承攬商 | 甲營造、乙機電、丙工程、丁營造… |
| 設備廠商 | 各門禁／監控設備商 | 設備商甲／乙／丙，代碼 `vendor-a`／`vendor-b`／`vendor-c` |
| 系統整合商 | 各實際整合商 | 系統整合商甲／乙 |
| 人員姓名 | 現場人員與主管 | 王小明、陳小華、林小美…（一望即知為虛構的化名） |
| 人員工號 | 門禁系統實際編號 | `EMP-001`、`EMP-081`… |

**缺失內容本身（例如「施工架踏板倚靠欄杆未移除」）不去識別化**，
因為那是工安管理知識、不指向特定個人，保留才能讓示範資料具參考價值。

---

## 三、每次推送的標準流程

```bash
# 1. 掃描（pre-commit hook 會自動執行，這裡是手動確認）
python tools/deidentify.py --check

# 2. 若有發現，自動置換
python tools/deidentify.py --apply <被標記的檔案>

# 3. 再掃一次，確認乾淨
python tools/deidentify.py --check

# 4. 更新本文件的「變更紀錄」

# 5. commit & push
git add -A && git commit -m "..." && git push
```

首次 clone 後需先啟用 hook（`core.hooksPath` 不會隨 repo 帶過來）：

```bash
git config core.hooksPath .githooks
```

未設定 `deid_rules.local.json` 時，工具會警告但不擋 commit
（避免新環境完全無法運作）；**負責推送的人必須確保本機有這個檔案**。

---

## 四、需要真實版本文件時

給廠商或主管看的對內版本（例如 API 規格書要寫實際廠商名），
用還原功能產生，輸出檔已被 `.gitignore` 排除：

```bash
python tools/deidentify.py --restore API_SPEC.md -o API_SPEC.internal.md
```

---

## 五、不進版控的內容

`.gitignore` 已排除下列項目，新增內容時請一併確認：

- `deid_rules.local.json` —— 還原金鑰
- `uploads/` —— 使用者上傳的照片、手寫簽名圖、產出的 PDF（全部含個資）
- `*.db` —— 資料庫檔案
- `*.docx` / `*.pdf` —— 公司內部文件、原始表單範本、紙本掃描件
- `*.internal*` / `docs.internal/` —— 還原後的對內版本
- `.env` —— 連線字串、金鑰、廠商權杖

### 注意：`data/forms.json`

此檔為公司自主檢查表的 541 個項目全文，屬公司內部標準文件（非個資）。
目前選擇保留在 repo 中，因為它是系統運作的必要資料且不含個人資訊。
**若日後公司認定不宜公開，將其加入 `.gitignore`，改由使用者自行執行
`tools/extract_forms.py` 從內部範本產生即可，程式不需修改。**

---

## 六、變更紀錄

每次推送 GitHub 時追加一列。

| 日期 | 內容 | 去識別化掃描 |
|---|---|---|
| 2026-08-17 | 專案初版：填報網站、PDF 簽核、戰情儀表板、對外 API | 建立機制，置換 99 處 |
| 2026-08-17 | 資料庫由 SQLite 改為 SQL Server LocalDB，加入索引與 `/api/health` | 通過 |
