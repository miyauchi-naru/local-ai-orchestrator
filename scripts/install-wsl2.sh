#!/usr/bin/env bash
# RTX Spark の WSL2（Ubuntu Arm64）内にエンジン群を用意する案（企画書 2.2 節の案 A）。
# 既定は表示のみ。--execute を付けたときだけ実行する。ユーザーの「開始」指示の後、実機で使う。
# TODO(P0): WSL2 内で CUDA が使えるか（nvidia-smi と PyTorch）を最初に確認する。使えなければこの案は不採用。
set -euo pipefail
EXECUTE=0
[[ "${1:-}" == "--execute" ]] && EXECUTE=1
ROOT="${LAO_ROOT:-$HOME/lao}"
LLAMA_REF="${LLAMA_REF:-master}"     # TODO(P0): 動作確認した版に固定
COMFY_REF="${COMFY_REF:-master}"     # TODO(P0)
REPO="$(cd "$(dirname "$0")/.." && pwd)"

step() {
  local title="$1"; shift
  echo "== $title"
  if [[ $EXECUTE == 1 ]]; then bash -c "$*"; else echo "   （確認のみ）$*"; fi
}

echo "CPU: $(uname -m)"
[[ "$(uname -m)" == "aarch64" ]] || echo "警告：aarch64 ではありません"
echo "メモリ: $(awk '/MemTotal/ {printf "%.0fGB", $2/1048576}' /proc/meminfo)（WSL2 の上限は Windows 側の .wslconfig の memory で決まる）"
if command -v nvidia-smi >/dev/null; then nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || true; else echo "警告：WSL2 内で nvidia-smi が使えません"; fi
if command -v ollama >/dev/null; then echo "警告：ollama が入っています。本ツールは使いません"; fi

step "パッケージ" "sudo apt-get update && sudo apt-get install -y build-essential cmake git ffmpeg python3-venv python3-pip"
step "フォルダ" "mkdir -p '$ROOT'/{models,engines,library,jobs,logs}"
step "本ツール（lao）" "python3 -m venv '$ROOT/venv' && '$ROOT/venv/bin/pip' install -e '$REPO'"
# TODO(P0): WSL2 Arm64 用の CUDA Toolkit の入手方法を確認（DGX Spark 向けの Linux Arm64 版が使えるか）
step "llama.cpp（CUDA）" "git clone https://github.com/ggml-org/llama.cpp '$ROOT/engines/llama.cpp-src' && git -C '$ROOT/engines/llama.cpp-src' checkout '$LLAMA_REF' && cmake -S '$ROOT/engines/llama.cpp-src' -B '$ROOT/engines/llama.cpp-src/build' -DGGML_CUDA=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release && cmake --build '$ROOT/engines/llama.cpp-src/build' --target llama-server llama-bench -j && mkdir -p '$ROOT/engines/llama.cpp' && cp -r '$ROOT/engines/llama.cpp-src/build/bin/'* '$ROOT/engines/llama.cpp/'"
# TODO(P0): Linux Arm64 + CUDA 13 の PyTorch ホイールの入手先を確認
step "ComfyUI と PyTorch（Linux Arm64 + CUDA）" "git clone https://github.com/comfyanonymous/ComfyUI '$ROOT/engines/comfyui' && git -C '$ROOT/engines/comfyui' checkout '$COMFY_REF' && python3 -m venv '$ROOT/engines/comfyui/.venv' && '$ROOT/engines/comfyui/.venv/bin/pip' install torch torchvision torchaudio && '$ROOT/engines/comfyui/.venv/bin/pip' install -r '$ROOT/engines/comfyui/requirements.txt'"
step "設定ファイル" "[ -f '$REPO/config/machine.toml' ] || sed -e 's#C:/lao#$ROOT#g' -e 's#runtime = \"native\"#runtime = \"wsl2\"#' -e 's#\\.exe##g' -e 's#Scripts/python#bin/python#' '$REPO/config/machine.example.toml' > '$REPO/config/machine.toml'"

echo
echo "次の手順：lao doctor → lao p0 run --gpu-python $ROOT/engines/comfyui/.venv/bin/python → lao p0 propose"
[[ $EXECUTE == 1 ]] || echo "（確認のみでした。実行するには --execute を付けてください）"
