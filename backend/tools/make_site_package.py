# -*- coding: utf-8 -*-
"""產生「工地檢視器」安裝包——工地普通電腦解壓即用，只需要網際網路。

最大原則（2026-09-02 與使用者定案）
----------------------------------
**工地一台普通電腦、沒有任何公司資料庫權限、只有網路，也要看得到儀表板。**

因此這個包完全自足，不連公司任何系統：

  * **免安裝**：內附 Python embeddable 與全部相依套件。不碰系統、
    不寫登錄檔、不需要管理員權限。
  * **自帶資料庫**：本機 SQLite。不用 SQL Server、不用唯讀帳號、
    不用找 IT 開防火牆。
  * **自己抓資料**：包內含收集程式，跑在工地就近抓——表單同步（雲端
    填報站）、環境數據（氣象站平台）、在場人數（人臉裝置）全在外網。
  * **列控表工項打包時烤入**：改版時寄一個小檔到工地，點「更新工項.cmd」。
  * **進度顯示「預定進度」**：依列控表工期推算，前端明確標示、不與
    實際進度混用（工地版不碰 FinOps，沒有實際值）。

包的內容是本 repo 的縮小版（backend/onprem ＋ frontend），程式碼只有
一份——修 bug 改一處、重新打包即可。

⚠ 打包時會把本機 `.env.onprem` 裡的雲端同步權杖、氣象站帳密、人臉裝置
位址代填進包（工地要用它們抓資料）。**包等同於這些憑證，不可外流**；
外流時換掉對應權杖／密碼即可，資料庫帳密不在其中。

用法（在有網路、有本機資料庫的開發機上執行）
--------------------------------------------
    python backend/tools/make_site_package.py
    python backend/tools/make_site_package.py -o D:/隨身碟/viewer.zip
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend", "onprem"))

from app.envfile import parse_env  # noqa: E402——與伺服器共用同一份 .env 解析規則

# 包內啟動檔的檔名。autostart.ps1 的排程目標也用這個常數組出來——
# 兩處各寫一份字串的話，改名只改到一處，排程會註冊成功但登入後
# 靜默啟動失敗（Register-ScheduledTask 不驗證目標存在）。
LAUNCH_CMD = "啟動儀表板.cmd"

# 檢視器會用到的收集程式。刻意不整包複製 collectors/：finops.py 與
# access.py 需要公司內網與 pyodbc（包內沒裝驅動），放進去只會讓人
# 誤跑然後對著 ModuleNotFoundError 檢討半天。
VIEWER_COLLECTORS = ("__init__.py", "config.py", "weather.py", "face.py",
                     "sync_forms.py")

# 打包用的 Python 版本。embeddable zip 與 pip 下載的 wheel 必須同一版，
# 改版號時兩處會一起變。3.12 是目前 wheel 生態最齊的穩定版。
PY_VER = "3.12.8"
PY_TAG = "312"
EMBED_URL = (f"https://www.python.org/ftp/python/{PY_VER}/"
             f"python-{PY_VER}-embed-amd64.zip")

# 從本機 .env.onprem 代填進包的設定——只搬工地抓資料要用的，
# 公司資料庫的帳密（FINOPS_*、ACCESS_*）與 SECRET_KEY 一律不搬。
FILL_KEYS = (
    "CLOUD_API_URL", "CLOUD_SYNC_TOKEN", "SYNC_INTERVAL",
    "WEATHER_API_URL", "WEATHER_API_USER", "WEATHER_API_PASS",
    "WEATHER_SITE_MAP", "WEATHER_INTERVAL",
    "FACE_URL", "FACE_SITE_MAP", "FACE_INTERVAL",
    "BRAND_NAME", "BRAND_SHORT_NAME", "BRAND_NAME_EN", "BRAND_GROUP",
    "SYSTEM_NAME", "WAR_ROOM_NAME", "PRIMARY_SITE_CODE", "BUILDING_LABELS",
    "CAM_URL", "CAM_USER", "CAM_PASS", "CAM_CHANNELS",
)

# cmd 檔內容一律純 ASCII：批次解析器對檔案編碼與 chcp 的互動是雷區
# （chcp 只影響執行期輸出，解析器讀中文時仍用當下的字碼頁），
# 中文放檔名與 ps1/txt 就好，cmd 內文不碰。
START_CMD = """\
@echo off
rem SafetyOps site viewer. Portable - just run this file.
rem One process = dashboard server + data collectors (site_runner.py).
chcp 65001 >nul
cd /d "%~dp0backend\\onprem"
set PYTHONIOENCODING=utf-8
rem Unbuffered: when output goes to a file Python block-buffers by default,
rem so a hard kill (antivirus / crash) loses the buffer and the log misses
rem where it died. Force line-by-line writes.
set PYTHONUNBUFFERED=1
if not exist "%~dp0logs" mkdir "%~dp0logs"
rem Rotate the log once it passes ~5 MB, keeping one previous copy. A crash
rem loop restarts every 10s and appends every time, so without this the
rem diagnostic log becomes the problem it was meant to diagnose.
set "LOG=%~dp0logs\\viewer.log"
for %%A in ("%LOG%") do if %%~zA GTR 5000000 (
  if exist "%LOG%.1" del "%LOG%.1"
  move /y "%LOG%" "%LOG%.1" >nul
)
rem The browser is opened by site_runner.py once the port is actually
rem listening - it is the side that knows the real port (VIEWER_PORT).
:loop
rem Log startup to a file: on a headless kiosk any crash is otherwise
rem invisible. If the dashboard never comes up, logs\viewer.log has why.
"%~dp0python\\python.exe" site_runner.py >> "%LOG%" 2>&1
rem Auto-restart on crash; wait so repeated failures do not spin the CPU.
timeout /t 10 /nobreak >nul
goto loop
"""

AUTOSTART_CMD = """\
@echo off
rem Register auto-start at logon. Run once; no admin rights needed.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1"
pause
"""

UPDATE_TASKS_CMD = """\
@echo off
rem Import an updated task schedule (*.internal.json placed in this folder).
rem Produce the file on the office machine with:
rem     python backend/tools/export_internal.py
chcp 65001 >nul
cd /d "%~dp0"
for %%f in ("%~dp0*.internal.json") do (
  "%~dp0python\\python.exe" "%~dp0backend\\tools\\import_internal.py" "%%f"
)
pause
"""

AUTOSTART_PS1 = f"""\
# 註冊「登入後自動啟動儀表板」的工作排程。非管理員可執行——
# -AtLogOn 必須指定使用者，不指定等於任何人登入都觸發，那需要管理員。
$pkg = $PSScriptRoot
$a = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument ('"{{0}}" "{{1}}"' -f (Join-Path $pkg 'background.vbs'),
                                (Join-Path $pkg '{LAUNCH_CMD}'))
$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME"
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'SafetyOps-Viewer' -Action $a -Trigger $t `
    -Settings $s -Force | Out-Null
Write-Host '已設定：登入後自動在背景啟動儀表板（工作排程 SafetyOps-Viewer）'
Write-Host '要取消：Unregister-ScheduledTask -TaskName SafetyOps-Viewer'
"""

# background.vbs 不在這裡內嵌字串——直接複製 backend/scripts/run_hidden.vbs，
# 隱藏啟動的修正（引號、錯誤處理）才不會只修到中央主機那份。

ENV_TEMPLATE = """\
# 工地檢視器設定。打包時已代填，一般不需要修改。
# 這個包完全自足：本機 SQLite、自己從外網抓資料，不連公司任何系統。

# 本機資料庫（不要改）
DB_BACKEND=sqlite

# 看板免登入。這台機器誰坐下都看得到儀表板——僅限工地辦公室內使用。
PUBLIC_DASHBOARD=true

# --- 以下由打包程式從開發機設定代填 ---
"""

README_TXT = """\
職安戰情儀表板－工地檢視器
==========================

免安裝。這個資料夾放哪裡都可以（桌面、D 槽皆可），不需要管理員權限、
不需要連公司內網——只要這台電腦上得了網路。

使用
----
1. 點兩下「啟動儀表板.cmd」——瀏覽器自動開啟儀表板
2. 要開機自動啟動：執行一次「安裝開機自動啟動.cmd」
3. 收到新的工項檔（.internal.json）時：把檔案放進本資料夾，
   點「更新工項.cmd」，重新整理儀表板即可

第一次啟動後，缺失與表單約一分鐘內出現（要先跟填報站同步一輪）、
環境數據與人數約十五分鐘內到齊。

看不到資料時
------------
確認這台電腦連得上網路。黑色視窗可以縮到最小，但不要關閉——
關閉等於關掉儀表板（用開機自動啟動就沒有這個視窗）。

畫面上的「預定進度」是依列控表工期推算的計畫值，
不是實際施工進度，僅供對照今日應進行的工項。
"""


def log(msg: str) -> None:
    # 主控台可能是 cp950，印不出的字元以 ? 代替，不讓記錄本身把打包弄掛
    text = f"[打包] {msg}"
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc))


def download(url: str, dest: str) -> None:
    log(f"下載 {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    log(f"  → {os.path.getsize(dest) / 1024 / 1024:.1f} MB")


def read_env_file() -> dict:
    """讀本機 .env.onprem 的鍵值（不經 os.environ，避免拿到殼層雜訊）。

    解析規則沿用 app/envfile.py 的 parse_env——同一份規則只存在一處。
    utf-8-sig：檔案被存成 UTF-8 (BOM) 時，純 utf-8 讀法會讓第一個鍵黏著
    U+FEFF，該設定就以「本機未設定」之姿靜默漏填進安裝包。
    """
    path = os.path.join(ROOT, ".env.onprem")
    if not os.path.exists(path):
        return {}
    return parse_env(io.open(path, encoding="utf-8-sig").read())


def main() -> None:
    ap = argparse.ArgumentParser(description="產生工地檢視器安裝包")
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "dist", "safetyops-viewer.zip"))
    # 稍後 make_archive 用 out[:-4] 去掉副檔名，路徑不是 .zip 結尾的話
    # 會默默把 zip 寫去被截斷的路徑，再在 getsize 炸開——先擋在門口
    args_preview = ap.parse_known_args()[0]
    if not args_preview.out.lower().endswith(".zip"):
        sys.exit(f"-o 必須以 .zip 結尾（收到：{args_preview.out}）")
    # 工作目錄：每次建置開一個新的，舊的盡力清、清不掉就算了。
    # 不重用同一個目錄——防毒會掃剛寫入的幾千個檔案，重用時 rmtree
    # 常撞上暫時鎖（WinError 5，實測連系統暫存區都會）。
    # 也刻意不放 repo 裡：repo 在 OneDrive 底下，同步鎖更兇。
    ap.add_argument("--work", default="", help="打包工作目錄（預設每次新開）")
    args = ap.parse_args()

    base = os.path.join(tempfile.gettempdir(), "safetyops-viewer-build")
    os.makedirs(base, exist_ok=True)
    for old in os.listdir(base):            # 盡力清舊建置，失敗不阻擋
        if old.startswith("run-"):          # 只清建置目錄，別動下載快取
            shutil.rmtree(os.path.join(base, old), ignore_errors=True)
    work = args.work or tempfile.mkdtemp(prefix="run-", dir=base)
    pkg = os.path.join(work, "safetyops-viewer")
    shutil.rmtree(pkg, ignore_errors=True)
    os.makedirs(pkg, exist_ok=True)

    # --- 1. Python embeddable -------------------------------------------
    # 下載快取放固定位置（工作目錄每次都是新的，放那裡等於每次重下載）
    embed_zip = os.path.join(base, f"python-{PY_VER}-embed-amd64.zip")
    if not os.path.exists(embed_zip):
        download(EMBED_URL, embed_zip)
    else:
        log(f"沿用已下載的 {os.path.basename(embed_zip)}")
    pydir = os.path.join(pkg, "python")
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(pydir)

    # ._pth 決定 embeddable Python 的 sys.path（它不理 PYTHONPATH）。
    # 路徑相對於 python/ 目錄：lib 是相依套件、backend\onprem 讓
    # site_runner 與 app 模組找得到。
    pth = os.path.join(pydir, f"python{PY_TAG}._pth")
    with io.open(pth, "w", encoding="ascii", newline="\r\n") as f:
        f.write(f"python{PY_TAG}.zip\n.\n..\\lib\n..\\backend\\onprem\n")

    # --- 2. 相依套件（下載 3.12 的 wheel，與 embeddable 同版）-----------
    lib = os.path.join(pkg, "lib")
    req = os.path.join(ROOT, "backend", "onprem", "requirements.txt")
    log("pip 下載相依套件（限定 wheel，不編譯）")
    # encoding 與 PYTHONIOENCODING 要一致地指定：pip 依環境變數決定輸出
    # 編碼、父程序又用主控台預設(cp950)解碼的話，光是「解讀失敗訊息」
    # 這件事本身就會拋 UnicodeDecodeError
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "--quiet", "--target", lib,
         "--python-version", PY_TAG, "--platform", "win_amd64",
         "--implementation", "cp", "--only-binary=:all:",
         "-r", req],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        sys.exit("pip 失敗：\n" + (r.stderr or r.stdout or "（無輸出）")[-2000:])

    # --- 3. 伺服器、收集程式、前端、更新工具 -----------------------------
    shutil.copytree(os.path.join(ROOT, "backend", "onprem", "app"),
                    os.path.join(pkg, "backend", "onprem", "app"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    coll_dst = os.path.join(pkg, "backend", "onprem", "collectors")
    os.makedirs(coll_dst, exist_ok=True)
    for name in VIEWER_COLLECTORS:
        shutil.copyfile(os.path.join(ROOT, "backend", "onprem", "collectors", name),
                        os.path.join(coll_dst, name))
    shutil.copyfile(os.path.join(ROOT, "backend", "onprem", "site_runner.py"),
                    os.path.join(pkg, "backend", "onprem", "site_runner.py"))
    shutil.copytree(os.path.join(ROOT, "frontend"),
                    os.path.join(pkg, "frontend"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    # 更新工項用（相對路徑推算依賴 backend/tools 這個位置，照搬）
    os.makedirs(os.path.join(pkg, "backend", "tools"), exist_ok=True)
    shutil.copyfile(os.path.join(ROOT, "backend", "tools", "import_internal.py"),
                    os.path.join(pkg, "backend", "tools", "import_internal.py"))

    # --- 4. 烤入工地主檔與列控表工項 -------------------------------------
    # SQLite 預設路徑＝backend/onprem/safety.db（db.py 的 _sqlite_url）。
    # 不烤的話「今日重點工項」在工地會永遠空白——列控表只在公司拿得到。
    baked = os.path.join(pkg, "backend", "onprem", "safety.db")
    log("烤入工地主檔與列控表工項")
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "backend", "tools", "bake_viewer_db.py"),
         baked],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        sys.exit(f"烤入失敗：\n{r.stdout}\n{r.stderr}")
    log("  " + (r.stdout.strip().splitlines() or ["（無輸出）"])[-1])

    # --- 5. 設定檔：範本＋從本機 .env.onprem 代填 ------------------------
    src_env = read_env_file()
    lines = [ENV_TEMPLATE]
    filled, missing = [], []
    for k in FILL_KEYS:
        if src_env.get(k):
            lines.append(f"{k}={src_env[k]}")
            filled.append(k)
        else:
            missing.append(k)
    env_text = "\n".join(lines) + "\n"
    log(f"已代填 {len(filled)} 項設定：{'、'.join(filled)}")
    if missing:
        log(f"（本機未設定，包內留空：{'、'.join(missing)}）")

    files = {
        LAUNCH_CMD: (START_CMD, "ascii"),
        "安裝開機自動啟動.cmd": (AUTOSTART_CMD, "ascii"),
        "更新工項.cmd": (UPDATE_TASKS_CMD, "ascii"),
        "autostart.ps1": (AUTOSTART_PS1, "utf-8-sig"),
        "連線設定.env": (env_text, "utf-8"),
        "讀我.txt": (README_TXT, "utf-8-sig"),
    }
    for name, (content, enc) in files.items():
        with io.open(os.path.join(pkg, name), "w",
                     encoding=enc, newline="\r\n") as f:
            f.write(content)
    # 隱藏啟動的 vbs 與中央主機共用同一份來源檔，不各留一份字串常數
    shutil.copyfile(os.path.join(ROOT, "backend", "scripts", "run_hidden.vbs"),
                    os.path.join(pkg, "background.vbs"))

    # --- 6. 壓縮 ---------------------------------------------------------
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)
    log("壓縮中…")
    shutil.make_archive(args.out[:-4], "zip", work, "safetyops-viewer")
    size = os.path.getsize(args.out) / 1024 / 1024
    log(f"完成 → {args.out}（{size:.0f} MB）")
    log("⚠ 包內含雲端同步權杖與氣象站帳密，等同憑證，不可外流。")


if __name__ == "__main__":
    main()
