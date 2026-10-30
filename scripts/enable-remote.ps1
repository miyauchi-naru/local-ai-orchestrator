<#
.SYNOPSIS
  別の PC（メイン PC）の Claude Code から、この PC の lao を SSH 越しに使えるようにする。

.DESCRIPTION
  既定は「何をするかを表示するだけ」。-Execute を付けたときだけ実行する（利用者の「開始」指示の後）。
  管理者の PowerShell で実行する。

  - OpenSSH サーバーを入れ、自動起動にする
  - 鍵でのログインだけを許し、パスワードでのログインを禁止する。ログインできる利用者もこの人だけにする
  - ファイアウォールは「プライベート」ネットワークの同じ LAN（LocalSubnet）からだけ 22 番を許す
  - メイン PC の公開鍵を登録する
  - 受け取り箱（$DataRoot\inbox）を作る（メイン PC から scp で送ったファイルを import_file で取り込む）
  - -AutostartServe を付けると、lao serve を起動するタスク（lao-serve）を登録する。
    既定は PC の起動時（サインインしなくてよい。Windows Update の再起動の後も戻る）とログオン時の両方で、
    落ちたら 1 分おきに 3 回まで起動し直す。-ServeAtLogonOnly でログオン時だけにする
    （起動時の形で GPU が使えなかったとき。docs/arrival.md 3 章で確かめる）
  - lao の画面を開くショートカット「lao」をデスクトップとスタートメニューに置く

  lao serve と ComfyUI は今まで通り 127.0.0.1 でだけ待ち受ける。LAN に開くのは SSH だけ。
  終わったらメイン PC から `ssh spark C:/lao/venv/Scripts/lao.exe mcp --check` で確かめる（docs/remote.md）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\enable-remote.ps1 -PublicKeyFile .\main-pc.pub            # 確認のみ
  powershell -ExecutionPolicy Bypass -File scripts\enable-remote.ps1 -PublicKeyFile .\main-pc.pub -Execute   # 実行
#>
param(
  [switch]$Execute,
  [string]$PublicKeyFile = "",
  [string]$PublicKey = "",        # 公開鍵の 1 行をそのまま渡すとき（-PublicKeyFile の代わり）
  [string]$Root = "C:\lao",       # lao の Python 環境がある場所
  [string]$DataRoot = "D:\lao",   # machine.toml の paths.root（受け取り箱を置く）
  [switch]$AutostartServe,
  [switch]$ServeAtLogonOnly
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$User = $env:USERNAME
$SshDir = "$env:ProgramData\ssh"
$Config = "$SshDir\sshd_config"
$Lao = "$Root\venv\Scripts\lao.exe"

function Step([string]$Title, [scriptblock]$Action) {
  Write-Host "== $Title" -ForegroundColor Cyan
  if ($Execute) { & $Action } else { Write-Host "   （確認のみ）$($Action.ToString().Trim())" }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Warning "管理者の PowerShell で実行してください"; if ($Execute) { exit 1 } }
$Key = ""
if ($PublicKey) {
  $Key = $PublicKey.Trim()
} elseif ($PublicKeyFile -and (Test-Path $PublicKeyFile)) {
  $Key = (Get-Content $PublicKeyFile -Raw).Trim()
}
if (-not $Key) {
  Write-Warning "メイン PC の公開鍵を -PublicKeyFile（例：id_ed25519.pub）か -PublicKey（1 行）で指定してください"
  if ($Execute) { exit 1 }
} else {
  if ($Key -notmatch '^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) ') { throw "公開鍵の形式ではありません：$PublicKeyFile" }
  if ($Key -match 'PRIVATE KEY') { throw "秘密鍵が指定されています。.pub の方を渡してください" }
}
# 管理者の利用者は administrators_authorized_keys が使われる（Windows の OpenSSH の決まり）
$KeyFile = if (Get-LocalGroupMember -SID "S-1-5-32-544" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*\$User" }) { "$SshDir\administrators_authorized_keys" } else { "$env:USERPROFILE\.ssh\authorized_keys" }

function Set-SshdOption([string]$Name, [string]$Value) {
  $lines = if (Test-Path $Config) { Get-Content $Config } else { @() }
  $pattern = "^\s*#?\s*$Name\s"
  if ($lines | Where-Object { $_ -match $pattern }) {
    $lines = $lines | ForEach-Object { if ($_ -match $pattern) { "$Name $Value" } else { $_ } }
  } else {
    # Match ブロックより前に置かないと効かないので、先頭に足す
    $lines = @("$Name $Value") + $lines
  }
  Set-Content -Path $Config -Value $lines -Encoding ascii
}

Step "OpenSSH サーバーを入れる" {
  $cap = Get-WindowsCapability -Online | Where-Object Name -like "OpenSSH.Server*"
  if ($cap.State -ne "Installed") { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
}
Step "sshd を自動起動にして起動する（設定ファイルを作るため一度起動する）" {
  Set-Service -Name sshd -StartupType Automatic
  Start-Service sshd
}
Step "鍵でのログインだけを許し、ログインできる利用者を $User に限る" {
  Copy-Item $Config "$Config.bak-lao" -Force
  Set-SshdOption "PubkeyAuthentication" "yes"
  Set-SshdOption "PasswordAuthentication" "no"
  Set-SshdOption "KbdInteractiveAuthentication" "no"
  Set-SshdOption "PermitEmptyPasswords" "no"
  Set-SshdOption "AllowUsers" $User
  & "$env:windir\System32\OpenSSH\sshd.exe" -t
  if ($LASTEXITCODE -ne 0) { Copy-Item "$Config.bak-lao" $Config -Force; throw "sshd_config が正しくないため元に戻しました" }
}
Step "メイン PC の公開鍵を登録する（$KeyFile）" {
  New-Item -ItemType Directory -Force -Path (Split-Path $KeyFile) | Out-Null
  $existing = if (Test-Path $KeyFile) { Get-Content $KeyFile } else { @() }
  if ($existing -notcontains $Key) { Add-Content -Path $KeyFile -Value $Key -Encoding ascii }
  if ($KeyFile -like "$SshDir*") {
    # 管理者用の鍵ファイルは Administrators と SYSTEM だけが触れるようにしないと無視される
    icacls.exe $KeyFile /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" | Out-Null
  }
}
Step "ファイアウォール：SSH はプライベートネットワークの同じ LAN からだけ許す" {
  $rule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
  if ($rule) {
    Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Profile Private -RemoteAddress LocalSubnet -Enabled True
  } else {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (lao, LAN only)" -Direction Inbound `
      -Protocol TCP -LocalPort 22 -Action Allow -Profile Private -RemoteAddress LocalSubnet | Out-Null
  }
  $category = (Get-NetConnectionProfile | Select-Object -First 1).NetworkCategory
  if ($category -ne "Private") { Write-Warning "今のネットワークは $category です。家の LAN なら「プライベート」に変えてください" }
}
Step "sshd を設定どおりに起動し直す" {
  Restart-Service sshd
}
Step "受け取り箱を作る（$DataRoot\inbox）" {
  New-Item -ItemType Directory -Force -Path "$DataRoot\inbox" | Out-Null
}
if ($AutostartServe) {
  $when = if ($ServeAtLogonOnly) { "ログオン時" } else { "PC の起動時（サインイン不要）とログオン時" }
  Step "$when に lao serve を起動するタスク（lao-serve）を登録する。落ちたら起動し直す" {
    $action = New-ScheduledTaskAction -Execute $Lao -Argument "serve" -WorkingDirectory $Repo
    $triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $User)
    if (-not $ServeAtLogonOnly) { $triggers += New-ScheduledTaskTrigger -AtStartup }
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
      -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    # S4U：パスワードを預けずに、サインインしていなくてもこの利用者として動かす（ネットワークの共有フォルダは使えない）
    $logon = if ($ServeAtLogonOnly) { "Interactive" } else { "S4U" }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$User" -LogonType $logon -RunLevel Limited
    Register-ScheduledTask -TaskName "lao-serve" -Action $action -Trigger $triggers -Settings $settings `
      -Principal $principal -Force | Out-Null
  }
}
Step "lao の画面を開くショートカット「lao」をデスクトップとスタートメニューに置く" {
  $shell = New-Object -ComObject WScript.Shell
  $edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
  if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
  # API キーはショートカットに書かず、開くたびに lao key で受け取る
  $open = "`$k = & '$Lao' key; Start-Process '$edge' ('--app=http://127.0.0.1:8710/?key=' + `$k)"
  foreach ($dir in [Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs")) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "lao.lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -WindowStyle Hidden -Command `"$open`""
    $lnk.WindowStyle = 7
    if (Test-Path $edge) { $lnk.IconLocation = "$edge,0" }
    $lnk.Save()
  }
}

Write-Host ""
if (-not $Execute) {
  Write-Host "（確認のみ。実行するには -Execute を付けて、管理者の PowerShell で実行します）" -ForegroundColor Yellow
} else {
  Write-Host "完了。この PC の名前：$env:COMPUTERNAME（メイン PC の ~/.ssh/config の HostName に使う）" -ForegroundColor Green
  Write-Host "次の手順：メイン PC から ssh spark $($Lao.Replace('\', '/')) mcp --check（docs/remote.md）" -ForegroundColor Green
}
