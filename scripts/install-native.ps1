<#
.SYNOPSIS
  RTX Spark（Windows 11 on Arm）にネイティブでエンジン群を用意する。

.DESCRIPTION
  既定は「何をするかを表示するだけ」。-Execute を付けたときだけ実行する。
  ユーザーの「開始」指示の後、実機で使う。ドライバや CUDA の更新はしない（別途承認）。
  TODO(P0) の行は、実機で確かめてから確定する。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-native.ps1            # 確認のみ
  powershell -ExecutionPolicy Bypass -File scripts\install-native.ps1 -Execute   # 実行
#>
param(
  [switch]$Execute,
  [string]$Root = "C:\lao",
  [string]$LlamaCppRef = "master",          # TODO(P0): 動作確認した版（タグ）に固定する
  [string]$ComfyUIRef = "master",           # TODO(P0): 動作確認した版に固定する
  [string]$TorchSpec = 'torch==2.14.0+cu134 torchvision==0.29.0+cu134 torchaudio==2.11.0+cu134',
  [string]$TorchIndex = 'https://pypi.nvidia.com/nvtorch_oot/'
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

function Step([string]$Title, [scriptblock]$Action) {
  Write-Host "== $Title" -ForegroundColor Cyan
  if ($Execute) { & $Action } else { Write-Host "   （確認のみ）$($Action.ToString().Trim())" }
}

# --- 前提の確認（常に実行。何も変更しない） ---
$arch = $env:PROCESSOR_ARCHITECTURE
Write-Host "CPU: $arch"
if ($arch -ne "ARM64") { Write-Warning "Arm64 ではありません。RTX Spark 以外では使えません"; if ($Execute) { exit 1 } }
$mem = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Write-Host "メモリ: ${mem}GB"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) { & nvidia-smi --query-gpu=name,driver_version --format=csv,noheader } else { Write-Warning "nvidia-smi が見つかりません（ドライバ未導入）" }
if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Warning "ollama が入っています。本ツールは使いません" }
$drive = (Split-Path -Qualifier $Root)
$free = [math]::Round((Get-PSDrive $drive.TrimEnd(':')).Free / 1GB)
Write-Host "空き容量 ${drive}: ${free}GB（推奨 500GB 以上）"

Step "フォルダを作る ($Root)" {
  foreach ($d in "models","engines","library","jobs","logs") { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
}

# Python は Arm64 版が必要（x64 版では CUDA 版 PyTorch が入らない）
Step "Python 3.12（Arm64）を入れる" {
  winget install --id Python.Python.3.12 --architecture arm64 --accept-package-agreements --accept-source-agreements
}
Step "ffmpeg を入れる" {
  # TODO(P0): Arm64 ネイティブ版の入手先を確認（winget の Gyan.FFmpeg が Arm64 を出していなければ別配布を使う）
  winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
}
Step "Git と CMake、Visual Studio Build Tools（C++、Arm64）を入れる" {
  winget install --id Git.Git --accept-package-agreements --accept-source-agreements
  winget install --id Kitware.CMake --accept-package-agreements --accept-source-agreements
  winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.ARM64 --includeRecommended"
}
Write-Host "注意：CUDA Toolkit 13.4（Windows Arm64、プレビュー）は NVIDIA 開発者サイトから手動で入れてください（ドライバ・CUDA の導入は別承認）" -ForegroundColor Yellow

Step "本ツール（lao）を入れる" {
  py -3.12 -m venv "$Root\venv"
  & "$Root\venv\Scripts\python.exe" -m pip install --upgrade pip
  & "$Root\venv\Scripts\python.exe" -m pip install -e $Repo
}

Step "llama.cpp を CUDA でビルドする" {
  # TODO(P0): Windows Arm64 + CUDA でビルドできるか確認。できなければ WSL2 案（install-wsl2.sh）か Vulkan を試す
  git clone https://github.com/ggml-org/llama.cpp "$Root\engines\llama.cpp-src"
  git -C "$Root\engines\llama.cpp-src" checkout $LlamaCppRef
  cmake -S "$Root\engines\llama.cpp-src" -B "$Root\engines\llama.cpp-src\build" -DGGML_CUDA=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF
  cmake --build "$Root\engines\llama.cpp-src\build" --config Release --target llama-server llama-bench -j
  New-Item -ItemType Directory -Force -Path "$Root\engines\llama.cpp" | Out-Null
  Copy-Item "$Root\engines\llama.cpp-src\build\bin\Release\*" "$Root\engines\llama.cpp\" -Recurse -Force
}

Step "ComfyUI と PyTorch（CUDA、Windows Arm64）を入れる" {
  git clone https://github.com/comfyanonymous/ComfyUI "$Root\engines\comfyui"
  git -C "$Root\engines\comfyui" checkout $ComfyUIRef
  py -3.12 -m venv "$Root\engines\comfyui\.venv"
  $py = "$Root\engines\comfyui\.venv\Scripts\python.exe"
  & $py -m pip install --upgrade pip
  & $py -m pip install $TorchSpec.Split(' ') --extra-index-url $TorchIndex
  & $py -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
  & $py -m pip install -r "$Root\engines\comfyui\requirements.txt"
}

Step "設定ファイルを作る" {
  if (-not (Test-Path "$Repo\config\machine.toml")) { Copy-Item "$Repo\config\machine.example.toml" "$Repo\config\machine.toml" }
}

Write-Host ""
Write-Host "次の手順：lao doctor → lao p0 run --gpu-python $Root\engines\comfyui\.venv\Scripts\python.exe → lao p0 propose" -ForegroundColor Green
if (-not $Execute) { Write-Host "（確認のみでした。実行するには -Execute を付けてください）" }
