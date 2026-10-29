<#
.SYNOPSIS
  コード実行（分析ツール）の箱を、専用の WSL ディストリビューション「lao-sandbox」として用意する。

.DESCRIPTION
  既定は「何をするかを表示するだけ」。-Execute を付けたときだけ実行する（利用者の「開始」指示の後）。
  箱の中では bubblewrap で通信とファイルを切ってコードを動かす。このディストリビューション自体も
  Windows のドライブを自動マウントせず、Windows のプログラムを呼べないようにする（/etc/wsl.conf）。
  パッケージを入れる間だけ、このディストリビューションはネットにつながる。AI のコードはつながらない。
  終わったら lao sandbox-check で確かめ、すべて合格なら machine.toml の [sandbox] enabled = true にする。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-sandbox.ps1            # 確認のみ
  powershell -ExecutionPolicy Bypass -File scripts\install-sandbox.ps1 -Execute   # 実行
#>
param(
  [switch]$Execute,
  [string]$Root = "C:\lao",
  [string]$Distro = "lao-sandbox",
  # TODO(P0): 使う版に固定し、SHA256 を確かめる
  [string]$RootfsUrl = "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.3-base-arm64.tar.gz",
  [string]$RootfsSha256 = ""
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

function Step([string]$Title, [scriptblock]$Action) {
  Write-Host "== $Title" -ForegroundColor Cyan
  if ($Execute) { & $Action } else { Write-Host "   （確認のみ）$($Action.ToString().Trim())" }
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Write-Warning "WSL がありません（wsl --install が必要。別途承認）"; if ($Execute) { exit 1 } }
$exists = (wsl.exe -l -q 2>$null) -replace "`0", "" | Where-Object { $_.Trim() -eq $Distro }
if ($exists) { Write-Host "$Distro は既にあります。入れ直すときは wsl --unregister $Distro の後に実行してください" }

$tar = Join-Path $Root "downloads\sandbox-rootfs.tar.gz"
$dir = Join-Path $Root "wsl\$Distro"

Step "土台の Ubuntu（最小構成）を取得" {
  New-Item -ItemType Directory -Force -Path (Split-Path $tar) | Out-Null
  Invoke-WebRequest -Uri $RootfsUrl -OutFile $tar
  if ($RootfsSha256) {
    $h = (Get-FileHash $tar -Algorithm SHA256).Hash
    if ($h -ne $RootfsSha256.ToUpper()) { throw "SHA256 が一致しません：$h" }
  } else { Write-Warning "SHA256 を確かめていません（TODO(P0)）" }
}
Step "WSL に取り込む（$Distro）" {
  if (-not $exists) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    wsl.exe --import $Distro $dir $tar --version 2
  }
}
Step "Python と計算用の部品、bubblewrap を入れる" {
  wsl.exe -d $Distro -u root --exec /bin/sh -c "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-numpy python3-pandas python3-matplotlib python3-openpyxl python3-docx python3-scipy bubblewrap util-linux coreutils fonts-ipafont-gothic && apt-get clean"
}
Step "実行用の利用者と作業フォルダを作る" {
  wsl.exe -d $Distro -u root --exec /bin/sh -c "id sandbox >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin sandbox; mkdir -p /home/sandbox/work && chown sandbox:sandbox /home/sandbox/work && chmod 700 /home/sandbox/work"
}
Step "Windows のドライブと Windows のプログラムを切る（/etc/wsl.conf）" {
  $conf = "[automount]`nenabled=false`nmountFsTab=false`n[interop]`nenabled=false`nappendWindowsPath=false`n[user]`ndefault=sandbox`n"
  $conf | wsl.exe -d $Distro -u root --exec /bin/sh -c "cat > /etc/wsl.conf"
  wsl.exe --terminate $Distro
}
Step "箱を確かめる" {
  & "$Root\venv\Scripts\lao.exe" sandbox-check
}
Write-Host "`n合格なら config\machine.toml の [sandbox] で enabled = true にしてください"
