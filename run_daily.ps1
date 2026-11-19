<#
    Daily runner. Intended for Windows Task Scheduler.

    Register once (runs every day at 12:05, paper mode):
        schtasks /Create /TN "BetAgent" /SC DAILY /ST 12:05 ^
          /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<path>\bet-agent\run_daily.ps1\"" ^
          /F

    Switch to live money only after weeks of paper results:
        edit config.json -> "mode": "live", "execution": "ticket" (or "betfair")
        run_daily.ps1 will then require -ConfirmLive to actually place anything.

    betPawa preset (TZS, own ledger, dry run until you change it):
        .\run_daily.ps1 -Config config.betpawa.json -ConfirmLive
#>

param(
    [ValidateSet("scan", "bet")][string]$Command = "bet",
    [string]$Config = "",
    [switch]$ConfirmLive
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "py" }

$stamp = Get-Date -Format "yyyyMMdd"
$logDir = Join-Path $root "reports"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "run-$stamp.log"

$args = @("main.py")
if ($Config) { $args += @("--config", $Config) }
$args += $Command
if ($ConfirmLive) { $args += "--yes" }

"[$(Get-Date -Format o)] starting: $python $($args -join ' ')" | Tee-Object -FilePath $log -Append

& $python @args *>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE

"[$(Get-Date -Format o)] exit code: $code" | Tee-Object -FilePath $log -Append
exit $code
