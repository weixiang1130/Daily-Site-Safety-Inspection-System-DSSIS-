# 字型檔

本專案**不依賴任何 CDN**（工地內網／離線環境必須可正常運作），
因此品牌字型採自行托管。請將下列 woff2 檔案放入本資料夾：

| 檔名 | 字型 | 用途 |
|---|---|---|
| `CormorantGaramond-Regular.woff2` | Cormorant Garamond 400 | 展示級大標 |
| `CormorantGaramond-SemiBold.woff2` | Cormorant Garamond 600 | 展示級大標 |
| `SpaceGrotesk-Medium.woff2` | Space Grotesk 500 | 標題、全大寫標籤 |
| `SpaceGrotesk-Bold.woff2` | Space Grotesk 700 | 標題 |
| `DMSans-Regular.woff2` | DM Sans 400 | 內文 |
| `DMSans-Medium.woff2` | DM Sans 500 | 內文強調 |
| `DMSans-Bold.woff2` | DM Sans 700 | 內文強調 |
| `DMMono-Regular.woff2` | DM Mono 400 | 數據、章節編號 |
| `DMMono-Medium.woff2` | DM Mono 500 | KPI 大數字 |
| `NotoSansTC-Regular.woff2` | Noto Sans TC 400 | 中文內文 |
| `NotoSansTC-Medium.woff2` | Noto Sans TC 500 | 中文 |
| `NotoSansTC-Bold.woff2` | Noto Sans TC 700 | 中文標題 |

檔名必須與上表一致，`static/brand.css` 的 `@font-face` 依此對應。

## 檔案不存在時會怎樣

不會壞掉。`@font-face` 指向的檔案 404 時，瀏覽器會自動回退到
`brand.css` 字體堆疊中的下一個字體（中文回退到微軟正黑體／PingFang TC，
英文回退到系統無襯線字體）。版面與色彩完全正常，只有字型調性與品牌規範有落差。

## 授權

上述字型均可自行托管，但**放入前請確認授權條款**，
特別是要推送到公開 repo 的情況。Noto Sans TC 檔案較大，
建議先做繁體中文子集化（subset）再放入，以縮短載入時間。
