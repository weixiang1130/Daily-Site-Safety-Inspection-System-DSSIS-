"""收集程式共用的設定讀取與記錄。

實作在 app/envfile.py——API 與收集程式讀的是同一個 .env.onprem，
兩邊各寫一份解析邏輯遲早會漂移（例如密碼含 # 的處理方式不同），
而漂移的症狀是「同一組設定在某一邊莫名其妙不生效」，很難查。

設定檔格式與各欄位說明見 .env.onprem.example。
"""

from app.envfile import ENV_FILE, env, load_env, log

__all__ = ["ENV_FILE", "env", "load_env", "log"]
