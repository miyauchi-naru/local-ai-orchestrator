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
  [string]$Root = "C:\lao",                 # モデル・エンジン・Python 環境（C：1TB）
  [string]$DataRoot = "D:\lao",            # 成果物・会話・ジョブ・ログ・ComfyUI の出力（D：2TB）
  [string]$LlamaCppRef = "master",          # TODO(P0): 動作確認した版（タグ）に固定する
  [string]$ComfyUIRef = "master",           # TODO(P0): 動作確認した版に固定する
  [string]$TorchSpec = 'torch==2.14.0+cu134 torchvision==0.29.0+cu134 torchaudio==2.11.0+cu134',
  [string]$TorchIndex = 'https://pypi.nvidia.com/nvtorch_oot/',
  # CUDA Toolkit 13.4（Windows Arm64）のインストーラー。指定すると無人で入れる（-s）。
  # TODO(P0): NVIDIA 開発者サイトの配布 URL と SHA256 を確かめ、ここで取得できるようにする
  [string]$CudaInstaller = "",
  [switch]$SkipLlamaCpp                     # llama.cpp のビルドを飛ばす（WSL2 案を試すときなど）
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

function Step([string]$Title, [scriptblock]$Action) {
  Write-Host "== $Title" -ForegroundColor Cyan
  if ($Execute) { & $Action } else { Write-Host "   （確認のみ）$($Action.ToString().Trim())" }
}
# winget で入れたものを、開き直さずにこの PowerShell から使えるようにする
function Update-PathFromRegistry {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Test-Cuda { return [bool]($env:CUDA_PATH -or (Get-Command nvcc -ErrorAction SilentlyContinue)) }

# --- 前提の確認（常に実行。何も変更しない） ---
$arch = $env:PROCESSOR_ARCHITECTURE
Write-Host "CPU: $arch"
if ($arch -ne "ARM64") { Write-Warning "Arm64 ではありません。RTX Spark 以外では使えません"; if ($Execute) { exit 1 } }
$mem = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Write-Host "メモリ: ${mem}GB"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) { & nvidia-smi --query-gpu=name,driver_version --format=csv,noheader } else { Write-Warning "nvidia-smi が見つかりません（ドライバ未導入）" }
if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Warning "ollama が入っています。本ツールは使いません" }
foreach ($pair in @(@($Root, "モデル用、推奨 500GB 以上"), @($DataRoot, "成果物用"))) {
  $drive = (Split-Path -Qualifier $pair[0])
  $psd = Get-PSDrive $drive.TrimEnd(':') -ErrorAction SilentlyContinue
  if (-not $psd) { Write-Warning "ドライブ $drive がありません（$($pair[1])）"; if ($Execute) { exit 1 }; continue }
  Write-Host "空き容量 ${drive}: $([math]::Round($psd.Free / 1GB))GB（$($pair[1])）"
}

Step "フォルダを作る（$Root にモデルとエンジン、$DataRoot に成果物と作業データ）" {
  foreach ($d in "models","engines") { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
  foreach ($d in "library","jobs","logs","inbox","comfyui\output","comfyui\input") { New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot $d) | Out-Null }
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
  winget install --id Microsoft.VisualStudio.2022.BuildTools --accept-package-agreements --accept-source-agreements --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.ARM64 --includeRecommended"
  Update-PathFromRegistry
}
# CUDA Toolkit は Build Tools の後に入れる（VS との連携が入るように）。llama.cpp のビルドより前に要る
if (Test-Cuda) {
  Write-Host "CUDA Toolkit: あり（$env:CUDA_PATH）"
} elseif ($CudaInstaller) {
  Step "CUDA Toolkit を入れる（$CudaInstaller、無人）" {
    Start-Process -FilePath $CudaInstaller -ArgumentList "-s" -Wait
    $env:CUDA_PATH = [Environment]::GetEnvironmentVariable("CUDA_PATH", "Machine")
  }
} else {
  Write-Warning "CUDA Toolkit 13.4（Windows Arm64）がありません。NVIDIA 開発者サイトのインストーラーを -CudaInstaller で渡すか、先に入れてください（ドライバは更新しない）"
}

Step "本ツール（lao）を入れる" {
  Update-PathFromRegistry
  if (-not (Test-Path "$Root\venv\Scripts\python.exe")) { py -3.12 -m venv "$Root\venv" }
  & "$Root\venv\Scripts\python.exe" -m pip install --upgrade pip
  & "$Root\venv\Scripts\python.exe" -m pip install -e $Repo
}

if ($SkipLlamaCpp) { Write-Host "llama.cpp のビルドは飛ばします（-SkipLlamaCpp）" } else {
Step "llama.cpp を CUDA でビルドする" {
  # TODO(P0): Windows Arm64 + CUDA でビルドできるか確認。できなければ WSL2 案（install-wsl2.sh）か Vulkan を試す
  # ここで止めずに ComfyUI・PyTorch へ進む（GPU の確保上限は PyTorch で測れるため）
  if (-not (Test-Cuda)) { Write-Warning "CUDA Toolkit が無いため llama.cpp をビルドしません。CUDA を入れてから、もう一度このスクリプトを実行してください（済んだ手順は飛ばします）"; return }
  if (-not (Test-Path "$Root\engines\llama.cpp-src\.git")) { git clone https://github.com/ggml-org/llama.cpp "$Root\engines\llama.cpp-src" }
  git -C "$Root\engines\llama.cpp-src" checkout $LlamaCppRef
  cmake -S "$Root\engines\llama.cpp-src" -B "$Root\engines\llama.cpp-src\build" -DGGML_CUDA=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF
  cmake --build "$Root\engines\llama.cpp-src\build" --config Release --target llama-server llama-bench -j
  New-Item -ItemType Directory -Force -Path "$Root\engines\llama.cpp" | Out-Null
  Copy-Item "$Root\engines\llama.cpp-src\build\bin\Release\*" "$Root\engines\llama.cpp\" -Recurse -Force
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$Root\engines\llama.cpp\llama-server.exe")) { Write-Warning "llama-server.exe ができませんでした。上の表示を記録し、WSL2 案を試します（docs/arrival.md）" }
}
}

Step "ComfyUI と PyTorch（CUDA、Windows Arm64）を入れる" {
  if (-not (Test-Path "$Root\engines\comfyui\.git")) { git clone https://github.com/comfyanonymous/ComfyUI "$Root\engines\comfyui" }
  git -C "$Root\engines\comfyui" checkout $ComfyUIRef
  if (-not (Test-Path "$Root\engines\comfyui\.venv\Scripts\python.exe")) { py -3.12 -m venv "$Root\engines\comfyui\.venv" }
  $py = "$Root\engines\comfyui\.venv\Scripts\python.exe"
  & $py -m pip install --upgrade pip
  & $py -m pip install $TorchSpec.Split(' ') --extra-index-url $TorchIndex
  & $py -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
  & $py -m pip install -r "$Root\engines\comfyui\requirements.txt"
}

Step "設定ファイルを作り、ffmpeg・llama-server の場所と固定の API キーを埋める" {
  if (-not (Test-Path "$Repo\config\machine.toml")) { Copy-Item "$Repo\config\machine.example.toml" "$Repo\config\machine.toml" }
  Update-PathFromRegistry
  & "$Root\venv\Scripts\lao.exe" configure --auto
}

Write-Host ""
Write-Host "次の手順：lao doctor → lao p0 run --gpu-python $Root\engines\comfyui\.venv\Scripts\python.exe → lao p0 propose" -ForegroundColor Green
if (-not $Execute) { Write-Host "（確認のみでした。実行するには -Execute を付けてください）" }
