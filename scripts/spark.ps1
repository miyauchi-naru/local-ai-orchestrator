<#
.SYNOPSIS
  メイン PC から Spark（lao）を使うための小さな道具。メイン PC の Claude Code が呼ぶ（利用者が打たなくてよい）。

.DESCRIPTION
  prepare-main-pc.ps1 が ~/.lao/spark.ps1 に置く。どれも SSH（Host spark）だけを使い、LAN に新しい口は開けない。

    put <パス...>        ファイルかフォルダを Spark の受け取り箱へ送る（送った名前を表示。ask_local の inbox や import_file に渡す）
    get <Spark のパス> [保存先]   Spark の成果物をメイン PC へ取ってくる（既定の保存先は ~/Downloads/lao）
    check               つながるかを順に確かめ、だめなら理由を出す（眠っている・名前が引けない・sshd・lao serve）
    wake                Spark を Wake-on-LAN で起こし、つながるまで待つ（有線 LAN で、機種が対応しているときだけ）
    open                lao の画面を開く（トンネルが無ければ張ってから）
    tunnel              lao の画面用のトンネル（127.0.0.1:8710）を張り続ける。切れたら張り直す（ログオン時のタスクが使う）
    backup              Spark の最新のバックアップを ~/Documents/lao-backup に取ってくる（毎日のタスクが使う）
    usage [日数]        この PC の Claude Code が使ったトークンを会話記録（~/.claude/projects）から日ごとに数え、
                        Spark へ送る（既定は直近 3 日。毎日のタスクと /spark-usage が使う。数を送るだけで会話の中身は送らない）

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 put .\report.pdf
  powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 get "D:/lao/library/0123456789ab/image.png"
#>
param(
  [Parameter(Position = 0)][ValidateSet("put", "get", "check", "wake", "open", "tunnel", "backup", "usage")][string]$Command = "check",
  [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Dir = Join-Path $HOME ".lao"
$Info = Join-Path $Dir "spark.json"      # prepare-main-pc.ps1 が書く（PC 名・最後の IP・MAC・lao の場所・受け取り箱）
$Spark = if (Test-Path $Info) { Get-Content $Info -Raw -Encoding UTF8 | ConvertFrom-Json } else { [pscustomobject]@{} }
function Get-Opt([string]$Name, [string]$Default) { if ($Spark.PSObject.Properties[$Name] -and $Spark.$Name) { $Spark.$Name } else { $Default } }
$Lao = Get-Opt "lao" "C:/lao/venv/Scripts/lao.exe"
$Inbox = Get-Opt "inbox" "D:/lao/inbox"
$Port = Get-Opt "port" "8710"
$SshOpts = @("-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=8")

function Test-Ssh { ssh @SshOpts spark "echo ok" 2>$null | Out-Null; return $LASTEXITCODE -eq 0 }
function Test-Tunnel { try { (New-Object Net.Sockets.TcpClient("127.0.0.1", [int]$Port)).Close(); return $true } catch { return $false } }

function Send-Wol {
  $mac = Get-Opt "mac" ""
  if (-not $mac) { Write-Warning "Spark の MAC アドレスが分かりません（prepare-main-pc.ps1 -SparkHost で記録します）"; return $false }
  $bytes = ($mac -split "[:-]") | ForEach-Object { [Convert]::ToByte($_, 16) }
  $packet = [byte[]](,0xFF * 6 + ($bytes * 16))
  $udp = New-Object Net.Sockets.UdpClient
  $udp.EnableBroadcast = $true
  foreach ($p in 7, 9) { [void]$udp.Send($packet, $packet.Length, [Net.IPEndPoint]::new([Net.IPAddress]::Broadcast, $p)) }
  $udp.Close()
  return $true
}

switch ($Command) {
  "put" {
    if (-not $Rest) { throw "送るファイルかフォルダを指定してください" }
    foreach ($p in $Rest) {
      $item = Get-Item -LiteralPath $p
      $args2 = @($SshOpts) + $(if ($item.PSIsContainer) { @("-r") } else { @() }) + @("-q", $item.FullName, "spark:$Inbox/")
      scp @args2
      if ($LASTEXITCODE -ne 0) { throw "$($item.Name) を送れませんでした（spark.ps1 check で確かめてください）" }
      Write-Output "送りました：$($item.Name)$(if ($item.PSIsContainer) { '/' })（受け取り箱の名前。ask_local の inbox か import_file に渡す）"
    }
  }
  "get" {
    if (-not $Rest) { throw "取ってくる Spark のパス（生成の結果に出た保存場所）を指定してください" }
    $src = $Rest[0].Replace("\", "/")
    $dst = if ($Rest.Count -gt 1) { $Rest[1] } else { Join-Path $HOME "Downloads\lao" }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    scp @SshOpts -q "spark:$src" $dst
    if ($LASTEXITCODE -ne 0) { throw "$src を取ってこられませんでした" }
    Write-Output "保存しました：$(Join-Path $dst (Split-Path $src -Leaf))"
  }
  "check" {
    $name = Get-Opt "host" "spark"
    $resolved = $null
    try { $resolved = [Net.Dns]::GetHostAddresses($name) | Select-Object -First 1 } catch {}
    if (-not $resolved) { Write-Output "NG：Spark の名前（$name）が引けません。最後に分かった IP は $(Get-Opt 'ip' '不明') です" }
    if (Test-Ssh) {
      Write-Output "OK：SSH でつながります"
      ssh @SshOpts spark "$Lao mcp --check"
      if ($LASTEXITCODE -ne 0) { Write-Output "NG：lao serve が応えません（Spark の再起動直後なら 1〜2 分待つ。続くなら Spark で Get-ScheduledTask lao-serve）" }
      Write-Output $(if (Test-Tunnel) { "OK：lao の画面のトンネル（127.0.0.1:$Port）があります" } else { "注意：lao の画面のトンネルがありません（spark.ps1 open で張ります）" })
      exit 0
    }
    $ping = if ($resolved) { Test-Connection -ComputerName $resolved -Count 1 -Quiet } else { $false }
    if ($ping) { Write-Output "NG：Spark は応えますが SSH が通りません（Spark の sshd、ネットワークが「プライベート」か）" }
    else { Write-Output "NG：Spark が応えません。眠っているか電源・LAN が切れています。spark.ps1 wake で起こせることがあります（有線 LAN のとき）" }
    exit 1
  }
  "wake" {
    if (Test-Ssh) { Write-Output "Spark は起きています"; exit 0 }
    if (-not (Send-Wol)) { exit 1 }
    Write-Output "起こす合図を送りました。つながるまで最長 2 分待ちます"
    for ($i = 0; $i -lt 24; $i++) { Start-Sleep -Seconds 5; if (Test-Ssh) { Write-Output "OK：起きました"; exit 0 } }
    Write-Output "NG：起きませんでした（Wi-Fi では起こせないことが多い。蓋を開けて電源ボタンを押してください）"
    exit 1
  }
  "tunnel" {
    # ログオン時のタスクが隠れた窓で動かす。切れても 10 秒後に張り直す
    while ($true) {
      if (-not (Test-Tunnel)) {
        ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=8 `
          -o ClearAllForwardings=yes -L "${Port}:127.0.0.1:${Port}" spark
      }
      Start-Sleep -Seconds 10
    }
  }
  "open" {
    if (-not (Test-Tunnel)) {
      Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" tunnel"
      for ($i = 0; $i -lt 15 -and -not (Test-Tunnel); $i++) { Start-Sleep -Seconds 1 }
      if (-not (Test-Tunnel)) { Write-Output "NG：トンネルを張れません（spark.ps1 check で確かめてください）"; exit 1 }
    }
    # API キーは保存せず、開くたびに Spark から受け取る（画面には出さない）
    $key = (ssh @SshOpts spark "$Lao key" 2>$null | Select-Object -Last 1)
    $url = "http://127.0.0.1:$Port/" + $(if ($key) { "?key=$($key.Trim())" } else { "" })
    $edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
    if (Test-Path $edge) { Start-Process $edge "--app=$url" } else { Start-Process $url }
    Write-Output "lao の画面を開きました"
  }
  "backup" {
    $dst = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "lao-backup"
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    $path = (ssh @SshOpts spark "$Lao backup" | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $path) { throw "Spark でバックアップを作れませんでした" }
    scp @SshOpts -q "spark:$($path.Trim().Replace('\', '/'))" $dst
    if ($LASTEXITCODE -ne 0) { throw "バックアップを取ってこられませんでした" }
    Get-ChildItem $dst -Filter "lao-*.zip" | Sort-Object Name -Descending | Select-Object -Skip 30 | Remove-Item -Force
    Write-Output "バックアップを保存しました：$dst\$(Split-Path $path.Trim() -Leaf)"
  }
  "usage" {
    $days = if ($Rest) { [int]$Rest[0] } else { 3 }
    $since = (Get-Date).Date.AddDays(-($days - 1))
    $sinceKey = $since.ToString("yyyy-MM-dd")
    $projects = Join-Path $HOME ".claude\projects"
    $byMsg = @{}   # 同じ返答が内容ごとに何行かに分かれて残るので、返答の id で 1 つにまとめる
    $calls = @{}
    if (Test-Path $projects) {
      Get-ChildItem $projects -Recurse -Filter *.jsonl | Where-Object LastWriteTime -ge $since | ForEach-Object {
        foreach ($line in [IO.File]::ReadLines($_.FullName)) {
          if ($line -notmatch '"type":"assistant"') { continue }
          try { $d = $line | ConvertFrom-Json } catch { continue }
          $m = $d.message
          if (-not $m -or -not $m.usage -or -not $d.timestamp) { continue }
          $ts = if ($d.timestamp -is [datetime]) { $d.timestamp.ToLocalTime() } else { [DateTimeOffset]::Parse($d.timestamp).LocalDateTime }
          $day = $ts.ToString("yyyy-MM-dd")
          if ($day -lt $sinceKey) { continue }
          $byMsg["$($m.id)"] = @{ day = $day; u = $m.usage }
          foreach ($c in @($m.content)) { if ($c.type -eq "tool_use" -and "$($c.name)" -like "mcp__lao__*") { $calls[$day] = 1 + [int]$calls[$day] } }
        }
      }
    }
    $agg = @{}
    foreach ($v in $byMsg.Values) {
      if (-not $agg[$v.day]) { $agg[$v.day] = [ordered]@{ date = $v.day; input_tokens = 0; output_tokens = 0; cache_read_input_tokens = 0; cache_creation_input_tokens = 0; messages = 0; lao_calls = 0 } }
      $a = $agg[$v.day]
      foreach ($k in "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") { $a[$k] += [long]$v.u.$k }
      $a.messages += 1
    }
    if (-not $agg.Count) { Write-Output "直近 $days 日の Claude Code の記録はありません"; exit 0 }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "lao-usage"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    foreach ($a in $agg.Values) {
      $a.lao_calls = [int]$calls[$a.date]
      [IO.File]::WriteAllText((Join-Path $tmp "$($a.date).json"), ($a | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
    }
    $remote = Get-Opt "usage" "D:/lao/usage/claude-code"
    $mk = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("New-Item -ItemType Directory -Force -Path '$remote' | Out-Null"))
    ssh @SshOpts spark "powershell -NoProfile -EncodedCommand $mk" | Out-Null
    scp @SshOpts -q (Get-ChildItem $tmp -Filter *.json | ForEach-Object FullName) "spark:$remote/"
    if ($LASTEXITCODE -ne 0) { throw "Spark へ送れませんでした（spark.ps1 check で確かめてください）" }
    foreach ($a in $agg.Values | Sort-Object { $_.date }) {
      Write-Output ("{0}：入力 {1:N0}、出力 {2:N0}、キャッシュ読み {3:N0}、キャッシュ書き {4:N0}、lao の呼び出し {5} 回" -f $a.date, $a.input_tokens, $a.output_tokens, $a.cache_read_input_tokens, $a.cache_creation_input_tokens, $a.lao_calls)
    }
    Write-Output "Spark に送りました（lao の usage か、画面のふりかえりで見られます）"
  }
}
