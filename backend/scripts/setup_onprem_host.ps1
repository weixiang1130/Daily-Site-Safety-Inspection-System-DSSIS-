<#
    地端主機一鍵安裝——把戰情儀表板搬到一台整天開著的正式主機。

    這台主機是什麼
    --------------
    整套系統唯一需要「自己跑程式」的機器。它必須同時滿足兩件事：

      * **在公司內網**——FinOps 資料庫與 NAS 列控表只有內網拿得到
      * **連得到網際網路**——填報站、人臉裝置、氣象站平台都在外網，
        而且要把儀表板快照推到雲端給工地看板

    工地辦公室的電腦兩件都不符合（連不到內網、什麼都不能裝），
    因此工地是用瀏覽器看雲端看板，不跑任何程式。見 docs/地端戰情室.md。

    用法
    ----
    以「系統管理員」身分開 PowerShell，切到 repo 目錄後執行：

        powershell -ExecutionPolicy Bypass -File backend\scripts\setup_onprem_host.ps1

    可重複執行：已存在的排程會被覆蓋，不會產生重複項目。
    加 -CheckOnly 只檢查環境、不做任何變更。
#>

[CmdletBinding()]
param(
    # 儀表板對外的埠。預設 8000，與 run_server.cmd 一致。
    [int]$Port = 8000,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
# 本檔在 backend\scripts\ 底下，repo 根目錄在上兩層
$Root    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Scripts = Join-Path $Root 'backend\scripts'
$Onprem  = Join-Path $Root 'backend\onprem'
$Hidden  = Join-Path $Scripts 'run_hidden.vbs'

$script:Failed = @()

function Say-Step([string]$n)  { Write-Host "`n[$n]" -ForegroundColor Cyan }
function Say-Ok  ([string]$m)  { Write-Host "  OK   $m" -ForegroundColor Green }
function Say-Warn([string]$m)  { Write-Host "  注意 $m" -ForegroundColor Yellow }
function Say-Bad ([string]$m)  { Write-Host "  失敗 $m" -ForegroundColor Red
                                 $script:Failed += $m }

Write-Host "SafetyOps 地端主機安裝" -ForegroundColor White
Write-Host "repo：$Root"
Write-Host "主機：$env:COMPUTERNAME"

# --- 1. 系統管理員 ---------------------------------------------------------
Say-Step '1/7 權限'
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Say-Ok '以系統管理員執行'
} else {
    Say-Warn '不是系統管理員——防火牆那一步會被跳過，其餘照做'
}

# --- 2. Python -------------------------------------------------------------
Say-Step '2/7 Python'
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    Say-Ok "$((& python --version 2>&1)) → $($py.Source)"
} else {
    Say-Bad ' 找不到 python。請先安裝 Python 3.11，安裝時務必勾選 Add to PATH'
}

# --- 3. 資料庫 -------------------------------------------------------------
# LocalDB 是「跟著登入使用者跑」的，主機沒人登入時排程會空轉。
# 正式主機請裝 SQL Server Express（開機就啟動的正式服務）。
Say-Step '3/7 資料庫'
$drivers = @()
if ($py) {
    # 不能直接 `... 2>$null`：$ErrorActionPreference='Stop' 之下，原生程式
    # 的 stderr 一經重導就變成終止性錯誤（實測 RemoteException），pyodbc
    # 還沒裝時整支腳本會死在這裡——而「還沒裝」正是新主機的常態。
    try { $drivers = & python -c "import pyodbc;[print(d) for d in pyodbc.drivers()]" 2>$null }
    catch { $drivers = @() }
}
if ($drivers -match 'for SQL Server') {
    Say-Ok "ODBC 驅動：$(($drivers | Where-Object { $_ -match 'for SQL Server' }) -join ', ')"
} else {
    Say-Warn ' 尚未偵測到 SQL Server ODBC 驅動（或 pyodbc 還沒裝，下一步會裝）'
}

$express = Get-Service -Name 'MSSQL$SQLEXPRESS' -ErrorAction SilentlyContinue
if ($express) {
    Say-Ok "SQL Server Express 已安裝，狀態 $($express.Status)"
    if ($express.StartType -ne 'Automatic') {
        Say-Warn ' 啟動類型不是「自動」，主機重開後資料庫不會自己起來'
    }
    Say-Warn ' 請確認 .env.onprem 內有一行：MSSQL_SERVER=.\SQLEXPRESS'
} else {
    Say-Warn @'
 沒有 SQL Server Express，將沿用 LocalDB。
       LocalDB 只在使用者登入後才啟動——整天開著的主機如果會登出，
       收集程式會靜靜地全部失敗，牆上不會有任何錯誤訊息。
       正式主機請改裝 SQL Server Express（免費），再於
       .env.onprem 設定 MSSQL_SERVER=.\SQLEXPRESS
'@
}

# --- 4. 套件 ---------------------------------------------------------------
Say-Step '4/7 Python 套件'
if ($CheckOnly) {
    Say-Warn ' CheckOnly，略過安裝'
} elseif ($py) {
    Push-Location $Onprem
    try {
        & python -m pip install --quiet -r requirements.txt
        if ($?) { Say-Ok 'requirements.txt' } else { Say-Bad ' requirements.txt 安裝失敗' }
        & python -m pip install --quiet -r requirements-mssql.txt
        if ($?) { Say-Ok 'requirements-mssql.txt' } else { Say-Bad ' requirements-mssql.txt 安裝失敗' }
    } finally { Pop-Location }
}

# --- 5. 設定檔與資料庫初始化 ------------------------------------------------
Say-Step '5/7 設定與資料庫'
$envFile = Join-Path $Root '.env.onprem'
if (Test-Path $envFile) {
    Say-Ok '.env.onprem 存在'
    $envText = Get-Content $envFile -Raw
    # 少了這兩個，工地看板就不會更新——而牆上不會顯示任何錯誤，
    # 只會停在最後一次成功的快照，很難察覺
    foreach ($k in 'CLOUD_API_URL', 'CLOUD_SYNC_TOKEN') {
        if ($envText -notmatch "(?m)^\s*$k\s*=\s*\S") {
            Say-Warn " $k 未設定，工地看板不會收到快照"
        }
    }
} else {
    Say-Bad @'
 找不到 .env.onprem。這份檔案含密碼與裝置位址，刻意不進版控，
       必須從原本那台機器手動複製過來（隨身碟或加密郵件）。
       範本見 .env.onprem.example
'@
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
if (-not $CheckOnly -and $py -and (Test-Path $envFile)) {
    Push-Location $Onprem
    try {
        & python -c "from app.db import init_db; init_db(); print('db ready')"
        if ($?) { Say-Ok '資料庫結構已建立' } else { Say-Bad ' 資料庫初始化失敗，看上方錯誤訊息' }
    } finally { Pop-Location }
}

# --- 6. 排程 ---------------------------------------------------------------
Say-Step '6/7 工作排程'

function Register-SafetyOpsTask {
    param([string]$Name, [string]$Cmd, [Microsoft.Management.Infrastructure.CimInstance[]]$Trigger)

    $target = Join-Path $Scripts $Cmd
    if (-not (Test-Path $target)) { Say-Bad " 找不到 $Cmd"; return }

    $action = New-ScheduledTaskAction -Execute 'wscript.exe' `
        -Argument ('"{0}" "{1}"' -f $Hidden, $target)
    # ExecutionTimeLimit 0＝不限時：伺服器是常駐的，有時限會被排程器砍掉
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $set -Force | Out-Null
    Say-Ok $Name
}

if ($CheckOnly) {
    Say-Warn ' CheckOnly，略過註冊'
} else {
    $every = { param($m) New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) `
                 -RepetitionInterval (New-TimeSpan -Minutes $m) }

    # -AtLogOn 必須指定使用者：不指定等於「任何人登入都觸發」，那需要
    # 系統管理員權限，非管理員執行會 Access denied（實測）
    Register-SafetyOpsTask 'SafetyOps-Server'    'run_server.cmd' `
        (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
    Register-SafetyOpsTask 'SafetyOps-Weather'   'run_weather.cmd'        (& $every 15)
    Register-SafetyOpsTask 'SafetyOps-Face'      'run_face.cmd'           (& $every 5)
    # 雲端看板是備援（正規做法是工地檢視器直連資料庫），只在明確開啟時排程
    $envText2 = if (Test-Path $envFile) { Get-Content $envFile -Raw } else { '' }
    if ($envText2 -match '(?m)^\s*WALL_ENABLED\s*=\s*true') {
        Register-SafetyOpsTask 'SafetyOps-Wallboard' 'run_push_wallboard.cmd' (& $every 5)
    } else {
        Say-Warn ' WALL_ENABLED 未開啟，略過雲端看板排程（備援用，見 docs/工地檢視器.md）'
        # 之前開過又關掉的話，舊排程要清掉——留著每 5 分鐘空轉一次，
        # 而且哪天推送程式的執行期檢查被改掉，它就默默重新開始外推
        if (Get-ScheduledTask -TaskName 'SafetyOps-Wallboard' -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName 'SafetyOps-Wallboard' -Confirm:$false
            Say-Warn ' 已移除先前註冊的 SafetyOps-Wallboard 排程'
        }
    }
    Register-SafetyOpsTask 'SafetyOps-Access'    'run_access.cmd'         (& $every 30)
    # 工地看板的資料來源：出工回報（LINE 群組經試算表）與職安署新知
    Register-SafetyOpsTask 'SafetyOps-Worklog'   'run_worklog.cmd'        (& $every 15)
    Register-SafetyOpsTask 'SafetyOps-OshaNews'  'run_osha_news.cmd'      (& $every 360)
    Register-SafetyOpsTask 'SafetyOps-Sync-AM'   'run_sync_forms.cmd' `
        (New-ScheduledTaskTrigger -Daily -At '07:00')
    Register-SafetyOpsTask 'SafetyOps-Sync-PM'   'run_sync_forms.cmd' `
        (New-ScheduledTaskTrigger -Daily -At '19:00')
    # 月結資料，一天一次已經過於頻繁，但成本極低且能自動補上改期的月結
    Register-SafetyOpsTask 'SafetyOps-Finops'    'run_finops.cmd' `
        (New-ScheduledTaskTrigger -Daily -At '06:30')
}

# --- 7. 防火牆 -------------------------------------------------------------
# 只對「私人／網域」放行，不對公用網路。這是給內網的戰情室大螢幕連的；
# 工地電腦看的是雲端看板，不會連到這個埠。
Say-Step '7/7 防火牆'
$ruleName = "SafetyOps-Dashboard-$Port"
if ($CheckOnly) {
    Say-Warn ' CheckOnly，略過'
} elseif (-not $isAdmin) {
    Say-Warn " 非管理員，未新增規則。之後請以管理員執行：
       New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound ``
         -Action Allow -Protocol TCP -LocalPort $Port -Profile Private,Domain"
} else {
    if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
        Say-Ok "$ruleName 已存在"
    } else {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Private,Domain `
            -Description '職安戰情室儀表板，僅公司內網可連' | Out-Null
        Say-Ok "$ruleName 已新增（僅私人／網域）"
    }
}

# --- 收尾 ------------------------------------------------------------------
Write-Host "`n────────────────────────────────" -ForegroundColor DarkGray
if ($script:Failed.Count) {
    Write-Host "有 $($script:Failed.Count) 項未完成：" -ForegroundColor Red
    $script:Failed | ForEach-Object { Write-Host "  ・$_" -ForegroundColor Red }
} else {
    Write-Host '全部完成。' -ForegroundColor Green
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
       Select-Object -First 1).IPAddress
Write-Host @"

內網大螢幕的網址：
    http://$env:COMPUTERNAME`:$Port/static/dashboard-detail.html
    http://$ip`:$Port/static/dashboard-detail.html

工地電腦看的是雲端看板，網址不同（見 docs/地端戰情室.md）：
    https://<你的站台>/static/dashboard-detail.html?k=<WALL_TOKEN>

還要做的事：
 1. 登出再登入一次，SafetyOps-Server 才會啟動（它掛在登入觸發）
 2. 電源設定改成「永不睡眠」，否則半夜資料會斷、工地看板會停在舊快照
 3. 確認雲端已設好 WALL_TOKEN，否則看板一律回 503
 4. 首次啟用先手動跑一次確認：
      cd backend\onprem
      python -m collectors.push_wallboard
"@
