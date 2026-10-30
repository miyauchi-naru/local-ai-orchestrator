<#
.SYNOPSIS
  メイン PC（Windows）の準備：SSH の鍵、Spark への接続設定、Claude Code への lao の登録（docs/remote.md）。

.DESCRIPTION
  既定は「何をするかを表示するだけ」。-Execute を付けたときだけ実行する（利用者の「開始」指示の後）。
  管理者でなくてよい。済んだ手順は飛ばすので、何度実行してもよい。

  -SparkHost を付けないとき（Spark が届く前にできる分）：
    - SSH の鍵（~/.ssh/id_ed25519）が無ければ作り、公開鍵の 1 行を表示してクリップボードに入れる
      → Spark の scripts\setup-spark.ps1 -PublicKey "<この 1 行>" に渡す（公開鍵なので会話に貼ってよい）
    - Claude Code の MCP の待ち時間を延ばす（~/.claude/settings.json の MCP_TOOL_TIMEOUT）
    - ~/.claude/CLAUDE.md に lao の使い方を足す（前の版があれば書き換える）
    - Claude Code のコマンド（/spark-image、/spark-video、/spark-summary、/spark-status、/spark-get、/spark-open、/spark-usage）を入れる
    - Spark とのやりとりの道具 ~/.lao/spark.ps1 を置き、Claude Code が確認なしで流せるようにする
  -SparkHost を付けたとき（Spark の setup-spark.ps1 が済んだ後）：
    - ~/.ssh/config に Host spark を足す（lao の画面用のトンネルは下のタスクが張る）
    - 初回の接続で Spark のホスト鍵を登録し、lao mcp --check でつながるか確かめる
    - Spark の IP と MAC アドレス（Wake-on-LAN 用）を ~/.lao/spark.json に記録する
    - Claude Code に lao を登録する（claude mcp add lao --scope user）
    - ログオン時に lao の画面用のトンネルを張り続けるタスク、毎日バックアップを取ってきて Claude Code のトークンの使用量を送るタスクを登録する
    - デスクトップとスタートメニューに「lao（Spark）」を置く（押すと lao の画面が開く）

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -AddClaudeMd -Execute
  powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -SparkHost SPARK-PC -SparkUser naru -Execute
#>
param(
  [switch]$Execute,
  [string]$SparkHost = "",          # Spark の PC 名か IP アドレス（setup-spark.ps1 が最後に表示する）
  [string]$SparkUser = "",          # Spark の利用者名
  [string]$SparkLao = "C:/lao/venv/Scripts/lao.exe",
  [switch]$AddClaudeMd              # 互換のため残す（今は常に足す）
)
$ErrorActionPreference = "Stop"
$SshDir = Join-Path $HOME ".ssh"
$KeyPath = Join-Path $SshDir "id_ed25519"
$SshConfig = Join-Path $SshDir "config"
$ClaudeDir = Join-Path $HOME ".claude"
$LaoDir = Join-Path $HOME ".lao"
$SparkPs1 = Join-Path $LaoDir "spark.ps1"
$Repo = Split-Path -Parent $PSScriptRoot

# BOM なしの UTF-8 で書く（Claude Code は BOM つきの JSON を読めないことがある）
function Write-Utf8([string]$Path, [string]$Text) { [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding $false)) }

function Step([string]$Title, [scriptblock]$Action) {
  Write-Host "== $Title" -ForegroundColor Cyan
  if ($Execute) { & $Action } else { Write-Host "   （確認のみ）$($Action.ToString().Trim())" }
}

foreach ($exe in "ssh", "ssh-keygen") {
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { Write-Warning "$exe がありません（設定 → システム → オプション機能 → OpenSSH クライアント）"; if ($Execute) { exit 1 } }
}
$HasClaude = [bool](Get-Command claude -ErrorAction SilentlyContinue)
if (-not $HasClaude) { Write-Warning "claude（Claude Code）が見つかりません。lao の登録は飛ばします" }

if (Test-Path "$KeyPath.pub") {
  Write-Host "SSH の鍵：あり（$KeyPath）"
} else {
  # BatchMode で使うため、パスフレーズなしの鍵にする（この PC の利用者だけが読める場所に置かれる）
  Step "SSH の鍵を作る（$KeyPath）" {
    New-Item -ItemType Directory -Force -Path $SshDir | Out-Null
    # 空のパスフレーズは PowerShell の版によって渡り方が変わるので、コマンド行をそのまま渡す
    Start-Process ssh-keygen -ArgumentList "-q -t ed25519 -N `"`" -f `"$KeyPath`" -C `"$env:USERNAME@$env:COMPUTERNAME`"" -Wait -NoNewWindow
  }
}
if (Test-Path "$KeyPath.pub") {
  $pub = (Get-Content "$KeyPath.pub" -Raw).Trim()
  Write-Host "公開鍵（Spark の setup-spark.ps1 -PublicKey に渡す 1 行）：" -ForegroundColor Green
  Write-Host $pub
  if ($Execute) { Set-Clipboard -Value $pub; Write-Host "（クリップボードに入れました）" }
}

Step "Claude Code の MCP の待ち時間を 15 分にする（動画の生成を待てるように）" {
  New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
  $f = Join-Path $ClaudeDir "settings.json"
  $s = if (Test-Path $f) { Get-Content $f -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
  if (-not $s.PSObject.Properties["env"]) { $s | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) }
  if ($s.env.PSObject.Properties["MCP_TOOL_TIMEOUT"]) { $s.env.MCP_TOOL_TIMEOUT = "900000" }
  else { $s.env | Add-Member -NotePropertyName MCP_TOOL_TIMEOUT -NotePropertyValue "900000" }
  if (Test-Path $f) { Copy-Item $f "$f.bak-lao" -Force }
  Write-Utf8 $f ($s | ConvertTo-Json -Depth 20)
}

Step "~/.claude/CLAUDE.md に lao の使い方を書く（前の版があれば書き換える）" {
  $f = Join-Path $ClaudeDir "CLAUDE.md"
  $text = if (Test-Path $f) { Get-Content $f -Raw -Encoding UTF8 } else { "" }
  $block = (Get-Content (Join-Path $PSScriptRoot "main-pc\CLAUDE-lao.md") -Raw -Encoding UTF8).Trim()
  # 前の版（印のない v1 か、印のある版）を見出しから次の見出しの手前まで置き換える。印を消した版は手で直したものとして残す
  $pattern = "(?ms)^## Spark（lao）の使い方\r?\n(?:(?!^## ).)*"
  $m = [regex]::Match($text, $pattern)
  if ($m.Success -and ($m.Value -match "lao-block" -or $m.Value -notmatch "<!--")) {
    $text = $text.Remove($m.Index, $m.Length).Insert($m.Index, $block + "`n`n")
  } elseif (-not $m.Success) {
    $text = $text.TrimEnd() + "`n`n" + $block + "`n"
  } else { Write-Host "   手で直した版があるので書き換えません" }
  if (Test-Path $f) { Copy-Item $f "$f.bak-lao" -Force }
  Write-Utf8 $f $text
}

Step "Claude Code のコマンド（/spark-*）を入れる（~/.claude/commands）" {
  $dst = Join-Path $ClaudeDir "commands"
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item (Join-Path $PSScriptRoot "main-pc\commands\spark-*.md") $dst -Force
}

Step "Spark とのやりとりの道具を置く（$SparkPs1）" {
  New-Item -ItemType Directory -Force -Path $LaoDir | Out-Null
  Copy-Item (Join-Path $PSScriptRoot "spark.ps1") $SparkPs1 -Force
}

Step "Claude Code が lao の道具と spark.ps1 を確認なしで使えるようにする（~/.claude/settings.json の permissions.allow）" {
  $f = Join-Path $ClaudeDir "settings.json"
  $s = if (Test-Path $f) { Get-Content $f -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
  if (-not $s.PSObject.Properties["permissions"]) { $s | Add-Member -NotePropertyName permissions -NotePropertyValue ([pscustomobject]@{}) }
  if (-not $s.permissions.PSObject.Properties["allow"]) { $s.permissions | Add-Member -NotePropertyName allow -NotePropertyValue @() }
  $rules = @("mcp__lao", "Bash(powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1:*)",
             "PowerShell(powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1:*)")
  $s.permissions.allow = @(@($s.permissions.allow) + ($rules | Where-Object { @($s.permissions.allow) -notcontains $_ }))
  if (Test-Path $f) { Copy-Item $f "$f.bak-lao" -Force }
  Write-Utf8 $f ($s | ConvertTo-Json -Depth 20)
}

if ($SparkHost) {
  if (-not $SparkUser) { Write-Warning "-SparkUser（Spark の利用者名）も指定してください"; if ($Execute) { exit 1 } }
  Step "~/.ssh/config に Host spark を足す（既にあれば飛ばす）" {
    $text = if (Test-Path $SshConfig) { Get-Content $SshConfig -Raw } else { "" }
    if ($text -match "(?m)^\s*Host\s+spark\s*$") {
      Write-Host "   Host spark は既にあります。HostName が $SparkHost か確かめてください"
    } else {
      $block = @(
        "", "Host spark",
        "  HostName $SparkHost",
        "  User $SparkUser",
        "  IdentityFile ~/.ssh/id_ed25519",
        "  BatchMode yes",
        "  ServerAliveInterval 30"
      ) -join "`n"
      Add-Content -Path $SshConfig -Value $block -Encoding ascii
    }
  }
  Step "初めての接続で Spark のホスト鍵を登録し、lao serve につながるか確かめる" {
    ssh -o StrictHostKeyChecking=accept-new -o ClearAllForwardings=yes spark $SparkLao mcp --check
    if ($LASTEXITCODE -ne 0) { Write-Warning "つながりません。docs/arrival.md の「困ったとき」を見てください" }
  }
  Step "Spark の IP と MAC アドレスを記録する（$LaoDir\spark.json。名前が引けないときと Wake-on-LAN に使う）" {
    $q = 'Get-NetAdapter -Physical | Where-Object Status -eq Up | ForEach-Object { $ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress; [pscustomobject]@{ name = $_.Name; mac = $_.MacAddress; ip = $ip; wired = ($_.MediaType -eq "802.3") } } | ConvertTo-Json -Compress'
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($q))
    $raw = ssh -o ClearAllForwardings=yes spark "powershell -NoProfile -EncodedCommand $enc"
    $nics = @($raw | Out-String | ConvertFrom-Json)
    $nic = ($nics | Where-Object wired | Select-Object -First 1)
    if (-not $nic) { $nic = $nics | Select-Object -First 1 }
    New-Item -ItemType Directory -Force -Path $LaoDir | Out-Null
    $info = [ordered]@{ host = $SparkHost; user = $SparkUser; ip = $nic.ip; mac = $nic.mac; wired = [bool]$nic.wired;
                        lao = $SparkLao; inbox = "D:/lao/inbox"; port = "8710" }
    Write-Utf8 (Join-Path $LaoDir "spark.json") ($info | ConvertTo-Json)
    Write-Host "   IP $($nic.ip)、MAC $($nic.mac)（$(if ($nic.wired) { '有線' } else { '無線。Wake-on-LAN はほぼ使えません' })）"
    try { [void][Net.Dns]::GetHostAddresses($SparkHost) }
    catch { Write-Warning "$SparkHost という名前が引けません。ルーターで Spark の IP（$($nic.ip)）を固定し、-SparkHost $($nic.ip) で流し直してください" }
  }
  Step "ログオン時に lao の画面用のトンネルを張り続けるタスク（lao-spark-tunnel）を登録する" {
    $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SparkPs1`" tunnel"
    $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $st = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "lao-spark-tunnel" -Action $a -Trigger $t -Settings $st -Force | Out-Null
    Start-ScheduledTask -TaskName "lao-spark-tunnel"
  }
  Step "毎日 Spark のバックアップを取ってきて、Claude Code のトークンの使用量を Spark へ送るタスク（lao-spark-backup、12:30。PC が切れていたら次に起きたとき）を登録する" {
    $a = @("backup", "usage") | ForEach-Object {
      New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SparkPs1`" $_" }
    $t = New-ScheduledTaskTrigger -Daily -At "12:30"
    $st = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -RunOnlyIfNetworkAvailable
    Register-ScheduledTask -TaskName "lao-spark-backup" -Action $a -Trigger $t -Settings $st -Force | Out-Null
  }
  Step "デスクトップとスタートメニューに「lao（Spark）」を置く" {
    $shell = New-Object -ComObject WScript.Shell
    $edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    foreach ($dir in [Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs")) {
      $lnk = $shell.CreateShortcut((Join-Path $dir "lao（Spark）.lnk"))
      $lnk.TargetPath = "powershell.exe"
      $lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SparkPs1`" open"
      $lnk.WindowStyle = 7
      if (Test-Path $edge) { $lnk.IconLocation = "$edge,0" }
      $lnk.Save()
    }
  }
  if ($HasClaude) {
    Step "Claude Code に lao を登録する（全プロジェクト共通）" {
      $listed = (claude mcp list 2>$null | Out-String)
      if ($listed -match "(?m)^lao:") { Write-Host "   lao は登録済みです" }
      else { claude mcp add lao --scope user '--' ssh -o ClearAllForwardings=yes spark $SparkLao mcp }  # '--' は引用しないと PowerShell の版によって消える
    }
  }
  Write-Host ""
  Write-Host "lao の画面：デスクトップの「lao（Spark）」か、Claude Code で /spark-open" -ForegroundColor Green
}

if (-not $Execute) { Write-Host "（確認のみ。実行するには -Execute を付けます）" -ForegroundColor Yellow }
