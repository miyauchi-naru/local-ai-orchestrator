# RTX Spark 入手前の準備

- 作成日：2026年10月28日
- 対象：企画書 v3.1（[plan.md](plan.md)）の P0（実機検証）に向けた準備
- 状態：実機は未入手。本書の手順は**実機が届き、ユーザーが「開始」と言ってから**行う。今は何もインストールしない。

## 1 購入時の注意

### 1.1 ノートかデスクトップか

| 項目 | ノート（例：Surface Laptop Ultra） | 小型デスクトップ（例：Surface RTX Spark Dev Box） |
|---|---|---|
| 価格 | 2,599.99ドルから | 5,999.99ドルから |
| 発売 | 2026年10月16日に発売済み | 11月出荷予定 |
| 電力 | 45〜80W（NVIDIA仕様） | 100Wの熱設計（Microsoft）。NVIDIAのデスクトップ仕様は140W |
| メモリ | 最大128GB（構成で選ぶ。購入時に128GBを必ず選択） | 128GB |
| ストレージ | 最大2TB | 公開情報では未確認 |
| WSL | 標準のWindows 11 | WSL・VS Code・PowerShell 7が導入済み |

**判断の目安：** 動画生成（E4）や長時間の生成を重視するなら、電力と冷却に余裕のあるデスクトップが有利です。ノートは持ち運べる代わりに、長い生成で発熱と電力の制限を受けやすく、バッテリー駆動中は性能が落ちます。ノートを選ぶ場合も、生成はAC電源につないで行う前提にします。

### 1.2 ストレージ容量

- 企画書の見積もりでは、四環境のモデルとエンジンだけで**約220〜420GB**です。これに生成した画像・音声・動画、モデルの入れ替え時の新旧並存が加わります。
- **2TBを推奨**します。1TBではOS・他のアプリと合わせて足りなくなる可能性が高いです。
- 内蔵が足りない場合は、USB4／Thunderbolt接続の外付けNVMe SSDにモデルを置く案があります（ロード時間は延びます。P0で測定）。

### 1.3 メモリについての注意

Microsoftは「GPUが使える最大量は構成と処理内容によって変わり、総量より少ない」と注記しています。128GBのすべてをAIに使えるわけではないため、企画書の初期予算96GBが実際に確保できるかはP0で測ります。

## 2 未確認点の現状（2026年10月28日時点）

| 項目 | 状況 | P0での扱い |
|---|---|---|
| PyTorch（CUDA） | **確認済み**。NVIDIAがWindows Arm64向けのCUDA対応ホイールを配布（torch 2.14.0+cu134）。ネイティブArm64版のPythonが必要 | 手順どおり導入し、GPUが見えるか確認 |
| WSL2内のCUDA | **未確認**。WSLはDev Boxに導入済みだが、CUDAが動くとの記載はない | 最初に試す。動けばDGX Spark向けの手順を流用できる |
| llama.cpp のCUDAビルド（Windows Arm64） | **未確認**。Microsoftは「今後提供予定」と記載 | 自前ビルドを試す。駄目ならWSL2かVulkanを試す |
| ComfyUI | **未確認**。「全てのPCで動く」との一般的な説明のみ | PyTorchが動けば導入を試す。カスタムノードは個別に確認 |
| Diffusers、TTS、動画エンジン | **未確認** | 候補ごとに導入を試す |
| ソースビルドが必要なパッケージ | cryptography、grpcio、PyArrow、tiktoken、OpenCV Python は Windows Arm64 でソースビルドが必要（NVIDIA案内） | 依存に含むエンジンは導入に時間がかかる前提で計画 |
| Linux用（DGX Spark用）のパッケージ | ネイティブWindowsのPythonでは**動かない**（NVIDIA案内） | WSL2案でのみ流用可能 |
| GPUが使えるメモリの上限 | **未確認**（総量より少ないとだけ記載） | 実測する |

## 3 今のうちにできること

- [ ] 購入する機種・メモリ（128GB）・ストレージ（2TB推奨）を決める。
- [ ] Hugging Face のアカウントを用意し、利用条件の同意が必要なモデル（Gemma 4 等）の同意を済ませておく。
- [ ] 評価用の素材を集める（4章）。特に、自分の声の録音と、非発話音（拍手、ドア、犬の鳴き声など）を含む短い動画。
- [ ] 他人の声や顔を参考素材に使う予定があれば、本人の同意を得ておく。
- [ ] 外付けSSDを使う場合は、USB4／Thunderbolt対応のNVMe SSDを用意する。

## 4 日本語の評価課題（案）

P0とカタログ登録時に、同じ課題で候補を比べます。合格基準はP0で実機の結果を見て数値化し、後から緩めません。

| 環境 | 課題 | 見る点 |
|---|---|---|
| E1 推論 | 日本語の論理問題・文章要約・手順の計画を各3問 | 正確さ、根拠、分からないことを分からないと言えるか |
| E1 画像 | 図表、手書きメモ、写真の説明を各2枚 | 読み取りの正確さ、日本語の文字認識 |
| E1 音声 | 自分の声の1〜3分の録音、雑音の多い録音 | 内容理解、話者の感情、背景音への言及 |
| E1 動画 | 30秒〜1分の動画（会話と非発話音を含む） | 「何秒頃に何が起きたか」、映像と音の対応（例：ドアが映った時に音がしたか） |
| E2 画像生成 | 指定どおりの構図・日本語の看板・参考画像の作風を守った修正 | 指示一致、破綻、修正指示への追従 |
| E3 音声生成 | 200字程度の日本語文（固有名詞・数字・漢字の読み分けを含む）の読み上げ | 読み間違い、自然さ、参考音声の話し方への近さ |
| E4 動画生成 | E2で作った画像から5秒の動画、音声付きと無音の両方 | 元画像との一貫性、動きの破綻、音声と映像の同期 |
| 共通 | 各課題のピークメモリ、初回応答、完了までの時間 | 予算96GB内に収まるか、待ち時間 |

## 5 実機が届いた日の検証手順（P0）

ユーザーの「開始」の指示後に行います。各ステップの結果を記録し、企画書の15章（P0で確定する事項）を埋めます。

### 1日目：機器の確認

1. Windowsの更新を済ませ、機種・メモリ（128GB）・ストレージ・ドライバの版を記録する。
2. 電源設定を最高性能にし、AC電源で作業する。
3. タスクマネージャーで、GPUの「共有GPUメモリ」の上限表示を記録する。

### 2〜3日目：実行基盤の判定（企画書 2.2節）

1. **ネイティブ案：** Arm64版のPython（Miniconda）を入れ、NVIDIAの手順でPyTorch（CUDA）を導入し、GPUが使えるか確認する。
2. **WSL2案：** WSL2にUbuntu（Arm64）を入れ、`nvidia-smi` とCUDAのサンプルが動くか確認する。
3. 両案で大きめのテンソルを確保し、GPUが実際に使えるメモリの上限を測る。
4. どちらを主経路にするか、この時点で仮決定する。

### 4〜7日目：エンジンの動作確認

| 順 | エンジン | 確認内容 |
|---|---|---|
| 1 | llama.cpp（llama-server） | CUDAでビルドできるか。gpt-oss-120b の速度とピークメモリ。Gemma 4 E4B と Qwen3-Omni の画像・音声入力 |
| 2 | ffmpeg | 動画からフレームと音声を取り出し、区間ごとに理解モデルへ渡せるか（企画書 3.2節） |
| 3 | ComfyUI | 導入できるか。画像生成・編集モデルが動くか |
| 4 | TTS | 日本語対応の候補が動くか、日本語の読み上げ品質 |
| 5 | 動画生成 | LTX-2.x と Wan 2.2 が動くか。5秒の動画の生成時間とピークメモリ |
| 6 | 長時間テスト | 30分以上の連続生成でGPUタイムアウト（既知の不具合）が起きるか、起きた後に復帰できるか |

### 2週目：まとめ

1. 4章の評価課題を各候補で実行し、結果を比べる。
2. 各環境の成立／不成立、主経路、標準構成を決め、カタログ初版を作る。
3. UIを既存OSS（例：Open WebUI）で済ませるか判断する。
4. 結果を企画書に反映し、P1の着手判断をユーザーと行う。

## 出典

- [NVIDIA フォーラム：RTX Spark 開発の始め方](https://forums.developer.nvidia.com/t/rtx-spark-getting-started-with-development/385976)：PyTorch のWindows Arm64 ホイール、ソースビルドが必要なパッケージ、Linux 用パッケージが使えないこと。
- [Windows ブログ：RTX Spark 発表](https://blogs.windows.com/windowsexperience/2026/05/31/introducing-a-powerful-new-chapter-for-windows-pcs-accelerated-by-nvidia-rtx-spark/)：llama.cpp 等は今後提供予定、GPUメモリ上限の説明。
- [Surface Laptop Ultra](https://www.microsoft.com/en-us/surface/devices/surface-laptop-ultra)：価格、最大2TB、GPUが使えるメモリは総量より少ないとの注記。
- [Surface RTX Spark Dev Box](https://www.microsoft.com/en-us/surface/devices/surface-rtx-spark-dev-box)：価格、128GB、WSL導入済み、100Wの熱設計。
- [NVIDIA RTX Spark 仕様](https://www.nvidia.com/en-us/products/rtx-spark/specifications/)：SKU、電力。
- [NVIDIA ブログ：CUDA 13.4](https://developer.nvidia.com/blog/cuda-toolkit-13-4-adds-windows-on-arm-support-and-greater-control-over-shared-gpus/)：Windows on Arm 対応。
- [GSMArena：Surface Laptop Ultra の価格と発売](https://www.gsmarena.com/microsoft_reveals_surface_laptop_ultra_price_and_availability-news-74953.php)
