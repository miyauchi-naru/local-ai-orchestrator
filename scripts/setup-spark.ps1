<#
.SYNOPSIS
  RTX Spark の到着後の作業を、上から順に一度で行う（docs/arrival.md の 3 章）。

.DESCRIPTION
  既定は「何をするかを表示するだけ」。-Execute を付けたときだけ実行する（利用者の「開始」指示の後）。
  管理者の PowerShell で実行する。Spark 上の Claude Code（Remote Control）が流す前提で、
  済んだ手順は飛ばすので、途中で止まっても同じコマンドをもう一度実行すれば続きから進む。

  段階（-Phase で一つだけ選べる。既定は all = power, install, remote, check）：
    power    AC 電源のとき、蓋を閉じても動き続けるようにする。ネットワークを「プライベート」にし、
             Wi-Fi の省電力を切り、Windows Update のアクティブ時間を設定する
    install  ドライブを確かめ、scripts\install-native.ps1 で導入する（設定ファイルの自動記入を含む）
    remote   scripts\enable-remote.ps1 でメイン PC から SSH で使えるようにする（-PublicKey が要る）
    check    lao doctor、GPU が確保できる量の測定（モデルなし）、lao serve の起動と lao mcp --check
    models   -Envs のモデルを取得して測り、測定値を machine.toml に採用する（all には含まない。
             カタログの配布元を確かめ、HF_TOKEN を入れてから）

  記録は $DataRoot\logs\setup-<日時>.txt に残す。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup-spark.ps1 -PublicKey "ssh-ed25519 AAAA... main-pc"            # 確認のみ
  powershell -ExecutionPolicy Bypass -File scripts\setup-spark.ps1 -PublicKey "ssh-ed25519 AAAA... main-pc" -Execute   # 実行
  powershell -ExecutionPolicy Bypass -File scripts\setup-spark.ps1 -Phase models -Envs e1 -Execute
#>
param(
  [switch]$Execute,
  [ValidateSet("all", "power", "install", "remote", "check", "models")]
  [string]$Phase = "all",
  [string]$PublicKey = "",          # メイン PC の公開鍵の 1 行（scripts\prepare-main-pc.ps1 が表示する）
  [string]$PublicKeyFile = "",
  [string]$Root = "C:\lao",
  [string]$DataRoot = "D:\lao",
  [string]$CudaInstaller = "",      # install-native.ps1 に渡す
  [string]$Envs = "e1",             # models で取得・測定する環境（例：e1,e2）
  [int]$ActiveHoursStart = 8,       # Windows Update が再起動しない時間帯（最長 18 時間）
  [int]$ActiveHoursEnd = 2,
  [switch]$ServeAtLogonOnly         # lao serve を起動時（サインイン不要）でなくログオン時だけ起動する（enable-remote.ps1）
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Lao = "$Root\venv\Scripts\lao.exe"
$GpuPython = "$Root\engines\comfyui\.venv\Scripts\python.exe"
function Want([string]$Name) { return $Phase -eq $Name -or ($Phase -eq "all" -and $Name -ne "models") }
function Step([string]$Title, [scriptblock]$Action) {
  Write-Host "== $Title" -ForegroundColor Cyan
  if ($Execute) { & $Action } else { Write-Host "   （確認のみ）$($Action.ToString().Trim())" }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Warning "管理者の PowerShell（管理者として開いたターミナル）で実行してください"; if ($Execute) { exit 1 } }

if ($Execute) {
  New-Item -ItemType Directory -Force -Path "$DataRoot\logs" | Out-Null
  Start-Transcript -Path "$DataRoot\logs\setup-$(Get-Date -Format yyyyMMdd-HHmmss).txt" | Out-Null
}

# --- 記録（常に表示。何も変更しない） ---
Write-Host "PC 名: $env:COMPUTERNAME　利用者: $env:USERNAME"
if ($env:USERNAME -match '[^A-Za-z0-9._-]') { Write-Warning "利用者名に半角英数字以外が含まれます。SSH やビルドで問題が出ることがあります" }
$os = Get-CimInstance Win32_OperatingSystem
Write-Host "Windows: $($os.Caption) $($os.Version)　メモリ: $([math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB))GB"
foreach ($d in "C", "D") {
  $v = Get-Volume -DriveLetter $d -ErrorAction SilentlyContinue
  if ($v) { Write-Host "ドライブ ${d}: $([math]::Round($v.Size / 1GB))GB（空き $([math]::Round($v.SizeRemaining / 1GB))GB、$($v.FileSystem)）" }
  else { Write-Warning "ドライブ ${d}: がありません。「ディスクの管理」で用意してください（C=モデル、D=成果物）" }
}
$net = Get-NetConnectionProfile | Select-Object -First 1
if ($net) { Write-Host "ネットワーク: $($net.Name)（$($net.NetworkCategory)、$($net.InterfaceAlias)）" }
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
  ForEach-Object { Write-Host "IP: $($_.IPAddress)（$($_.InterfaceAlias)）" }

if (Want "power") {
  Step "AC 電源のとき：蓋を閉じても何もしない、スリープ・休止状態にしない（バッテリーのときは今のまま）" {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change monitor-timeout-ac 5
    powercfg /setactive SCHEME_CURRENT
    $q = powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION | Select-String "AC"
    Write-Host "   $q"
  }
  Step "今のネットワーク（$($net.Name)）を「プライベート」にする（家の LAN で実行すること）" {
    Get-NetConnectionProfile | Where-Object NetworkCategory -ne "DomainAuthenticated" |
      Set-NetConnectionProfile -NetworkCategory Private
  }
  Step "ネットワーク アダプターの省電力（電源を切ってよい）を外す" {
    foreach ($a in Get-NetAdapter -Physical | Where-Object Status -eq "Up") {
      try { Set-NetAdapterPowerManagement -Name $a.Name -AllowComputerToTurnOffDevice Disabled -ErrorAction Stop; Write-Host "   $($a.Name)：外しました" }
      catch { Write-Host "   $($a.Name)：この機種では変えられません（$($_.Exception.Message)）" }
    }
  }
  Step "有線 LAN のアダプターで Wake-on-LAN（マジック パケット）を有効にする（メイン PC の spark.ps1 wake で起こせるように）" {
    foreach ($a in Get-NetAdapter -Physical | Where-Object MediaType -eq "802.3") {
      try {
        Set-NetAdapterPowerManagement -Name $a.Name -WakeOnMagicPacket Enabled -ErrorAction Stop
        Write-Host "   $($a.Name)：有効にしました"
      } catch { Write-Host "   $($a.Name)：この機種では変えられません（$($_.Exception.Message)）" }
    }
  }
  Step "Windows Update のアクティブ時間を ${ActiveHoursStart} 時〜${ActiveHoursEnd} 時にする（この間は勝手に再起動しない）" {
    $k = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    New-Item -Path $k -Force | Out-Null
    Set-ItemProperty -Path $k -Name ActiveHoursStart -Value $ActiveHoursStart -Type DWord
    Set-ItemProperty -Path $k -Name ActiveHoursEnd -Value $ActiveHoursEnd -Type DWord
  }
}

if (Want "install") {
  Write-Host "== 導入（scripts\install-native.ps1）" -ForegroundColor Cyan
  $args2 = @{ Root = $Root; DataRoot = $DataRoot }
  if ($CudaInstaller) { $args2.CudaInstaller = $CudaInstaller }
  if ($Execute) { $args2.Execute = $true }
  & "$PSScriptRoot\install-native.ps1" @args2
}

if (Want "remote") {
  Write-Host "== メイン PC から使えるようにする（scripts\enable-remote.ps1）" -ForegroundColor Cyan
  $args3 = @{ Root = $Root; DataRoot = $DataRoot; AutostartServe = $true }
  if ($PublicKey) { $args3.PublicKey = $PublicKey }
  if ($PublicKeyFile) { $args3.PublicKeyFile = $PublicKeyFile }
  if ($Execute) { $args3.Execute = $true }
  if ($ServeAtLogonOnly) { $args3.ServeAtLogonOnly = $true }
  if (-not $PublicKey -and -not $PublicKeyFile) { Write-Warning "-PublicKey が無いので、この段階は飛ばします（メイン PC で scripts\prepare-main-pc.ps1 を実行すると表示されます）" }
  else { & "$PSScriptRoot\enable-remote.ps1" @args3 }
}

if (Want "check") {
  Step "点検（lao doctor）。NG があっても続けます" {
    & $Lao doctor
    if ($LASTEXITCODE -ne 0) { Write-Warning "lao doctor に NG があります（モデル未取得の NG はこの時点では正常です）" }
  }
  Step "GPU が確保できる量を測る（モデルなし）" {
    if (Test-Path $GpuPython) { & $Lao p0 run --gpu-python $GpuPython --skip-models }
    else { Write-Warning "$GpuPython がありません（ComfyUI・PyTorch の導入を先に）" }
  }
  Step "lao serve を起動し（登録したタスク lao-serve）、つながるか確かめる" {
    if (Get-ScheduledTask -TaskName "lao-serve" -ErrorAction SilentlyContinue) {
      Start-ScheduledTask -TaskName "lao-serve"
      Start-Sleep -Seconds 15
      & $Lao mcp --check
    } else { Write-Warning "タスク lao-serve がありません（remote の段階が済んでいません）" }
  }
}

if (Want "models") {
  if (-not $env:HF_TOKEN) { Write-Warning "HF_TOKEN がありません。利用条件への同意が要るモデルは取れません" }
  foreach ($e in $Envs.Split(",")) {
    $e = $e.Trim()
    Write-Host "== $e のモデル：取得計画" -ForegroundColor Cyan
    & $Lao plan $e
    Step "$e のモデルを取得する（C:\lao\models）" { & $Lao install $e --execute }
  }
  Step "モデルを測り、測定値を machine.toml に採用する" {
    & $Lao p0 run --gpu-python $GpuPython --envs $Envs
    & $Lao p0 propose
    & $Lao configure --apply-proposed
  }
}

Write-Host ""
if ($Execute) {
  Stop-Transcript | Out-Null
  Write-Host "メイン PC で：scripts\prepare-main-pc.ps1 -SparkHost $env:COMPUTERNAME -SparkUser $env:USERNAME -Execute" -ForegroundColor Green
} else {
  Write-Host "（確認のみ。実行するには -Execute を付けて、管理者の PowerShell で実行します）" -ForegroundColor Yellow
}
