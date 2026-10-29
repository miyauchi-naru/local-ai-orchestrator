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
    - -AddClaudeMd で ~/.claude/CLAUDE.md に lao の使い方を足す
  -SparkHost を付けたとき（Spark の setup-spark.ps1 が済んだ後）：
    - ~/.ssh/config に Host spark（LocalForward 8710 つき）を足す
    - 初回の接続で Spark のホスト鍵を登録し、lao mcp --check でつながるか確かめる
    - Claude Code に lao を登録する（claude mcp add lao --scope user）

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -AddClaudeMd -Execute
  powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -SparkHost SPARK-PC -SparkUser naru -Execute
#>
param(
  [switch]$Execute,
  [string]$SparkHost = "",          # Spark の PC 名か IP アドレス（setup-spark.ps1 が最後に表示する）
  [string]$SparkUser = "",          # Spark の利用者名
  [string]$SparkLao = "C:/lao/venv/Scripts/lao.exe",
  [switch]$AddClaudeMd
)
$ErrorActionPreference = "Stop"
$SshDir = Join-Path $HOME ".ssh"
$KeyPath = Join-Path $SshDir "id_ed25519"
$SshConfig = Join-Path $SshDir "config"
$ClaudeDir = Join-Path $HOME ".claude"

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

if ($AddClaudeMd) {
  Step "~/.claude/CLAUDE.md に lao の使い方を足す（既にあれば飛ばす）" {
    $f = Join-Path $ClaudeDir "CLAUDE.md"
    $text = if (Test-Path $f) { Get-Content $f -Raw } else { "" }
    if ($text -notmatch "## Spark（lao）の使い方") {
      $block = @(
        "", "## Spark（lao）の使い方",
        "- 画像・動画・読み上げ音声は lao の道具で作る。結果は ref と保存場所だけ受け取る",
        "- 作った画像・動画の確認は、まず lao の describe_media に任せる。自分で見るのは view で、必要なときだけ",
        "- 長い文書の要約・下書き・翻訳・画像用の英語プロンプト作りは lao の ask_local に任せ、結果だけ使う"
      ) -join "`n"
      Write-Utf8 $f ($text + $block + "`n")
    }
  }
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
        "  ServerAliveInterval 30",
        "  LocalForward 8710 127.0.0.1:8710"
      ) -join "`n"
      Add-Content -Path $SshConfig -Value $block -Encoding ascii
    }
  }
  Step "初めての接続で Spark のホスト鍵を登録し、lao serve につながるか確かめる" {
    ssh -o StrictHostKeyChecking=accept-new -o ClearAllForwardings=yes spark $SparkLao mcp --check
    if ($LASTEXITCODE -ne 0) { Write-Warning "つながりません。docs/arrival.md の「困ったとき」を見てください" }
  }
  if ($HasClaude) {
    Step "Claude Code に lao を登録する（全プロジェクト共通）" {
      $listed = (claude mcp list 2>$null | Out-String)
      if ($listed -match "(?m)^lao:") { Write-Host "   lao は登録済みです" }
      else { claude mcp add lao --scope user '--' ssh -o ClearAllForwardings=yes spark $SparkLao mcp }  # '--' は引用しないと PowerShell の版によって消える
    }
  }
  Write-Host ""
  Write-Host "ブラウザで lao の画面を使うとき：ssh -N spark を動かしたまま、Edge で http://127.0.0.1:8710/?key=<Spark の machine.toml の api_key>" -ForegroundColor Green
}

if (-not $Execute) { Write-Host "（確認のみ。実行するには -Execute を付けます）" -ForegroundColor Yellow }
