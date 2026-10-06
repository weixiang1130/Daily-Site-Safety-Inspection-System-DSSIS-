# 出工資料庫：產生某個工地 Apps Script 出口的通行碼。
# 寫進 .env.onprem（主場站寫 WORKLOG_SHEET_URL，其他工地寫 WORKLOG_URL_<工地代碼>），
# 同時複製到剪貼簿，供貼進該工地 Apps Script 第一行的 const TOKEN。不在畫面顯示。
#
#   powershell -ExecutionPolicy Bypass -File backend\tools\make_worklog_token.ps1 -Site BD10 -DeployId AKfy...
#   （-Primary 表示主場站，寫 WORKLOG_SHEET_URL）
#
# 通行碼由這支在本機產生、直接寫進設定檔：不經過對話、不經過任何人轉貼。
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_-]{1,32}$')][string]$Site,
  [Parameter(Mandatory = $true)][ValidatePattern('^AKfy[A-Za-z0-9_-]{20,}$')][string]$DeployId,
  [switch]$Primary
)
$ErrorActionPreference = 'Stop'
$envPath = Join-Path $PSScriptRoot '..\..\.env.onprem'
if (-not (Test-Path $envPath)) { throw "找不到 $envPath" }

$chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$token = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })

$key = if ($Primary) { 'WORKLOG_SHEET_URL' } else { "WORKLOG_URL_$Site" }
$line = "$key=https://script.google.com/macros/s/$DeployId/exec?token=$token"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$text = [System.IO.File]::ReadAllText((Resolve-Path $envPath), $utf8)
$pattern = "(?m)^$key=[^\r\n]*$"
$hits = [regex]::Matches($text, $pattern).Count
if ($hits -gt 1) { throw "在 .env.onprem 找到 $hits 行 $key，未修改任何東西" }
if ($hits -eq 1) {
  $text = [regex]::Replace($text, $pattern, $line.Replace('$', '$$'))
} else {
  if (-not $text.EndsWith("`n")) { $text += "`n" }
  $text += "# 出工回報來源（$Site）：Apps Script 唯讀出口。等同資料存取權，勿外流。`n$line`n"
}
[System.IO.File]::WriteAllText((Resolve-Path $envPath), $text, $utf8)

Set-Clipboard -Value $token
Write-Host ''
Write-Host "完成：$Site 的通行碼已寫進 .env.onprem（$key），並已複製到剪貼簿（不顯示在畫面上）。" -ForegroundColor Green
Write-Host '接著到該工地的 Apps Script 第一行，把引號裡的內容換成 Ctrl+V 貼上的內容，存檔後「管理部署作業→編輯→新版本→部署」。'
Write-Host '最後到 GitHub → Settings → Secrets 更新同名的 Secret（值是 .env.onprem 那一行等號後面的整串）。'
