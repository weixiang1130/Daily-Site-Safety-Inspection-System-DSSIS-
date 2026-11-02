# 字型檔

網頁走系統字體堆疊，`brand.css` 不載入字型檔，本資料夾沒有字型。
要放品牌字型時，把 woff2 放進來並在 `brand.css` 加回 `@font-face`，檔案不存在會讓每頁多打一串 404。

`netlify.toml` 已替 `/static/fonts/*` 設好長期快取。PDF 用的中文字型在 `backend/cloud/assets/fonts/`，兩者獨立。
