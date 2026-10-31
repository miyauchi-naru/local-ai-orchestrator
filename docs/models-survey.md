# 商用利用できるモデルの調査（2026-10-31）

ゲーム素材と、収益化する YouTube 動画に使うため、**重みのライセンスが商用利用を認めているもの**だけを候補にした。
RTX Spark（128GB、モデルに使うのは約 96GB）、Windows on Arm、llama.cpp と ComfyUI で動くことが前提。
［確］は配布元のページでライセンスまで読んだもの、［二］は解説記事などで、ダウンロード前に配布元で確かめる。

## 結論

| 役割 | 今のカタログ | 今回の判断 |
|---|---|---|
| ComfyUI を動かす会話モデル | Gemma 4 26B-A4B ほか | **Qwen3.6-35B-A3B・Qwen3.8-27B・Gemma 4 31B・Mistral Small 4・LFM2.5 を評価候補に追加**。決めるのは実機の `lao p0 pairs` |
| 大型の推論役 | gpt-oss-120b | 据え置き。対抗に Mistral Small 4（画像も見られて速い）を追加 |
| 音声まで聞くモデル | Qwen3-Omni-30B-A3B | 据え置き。新しい Qwen3.5-Omni は非公開 |
| 画像生成・編集 | Qwen-Image-2512／Edit-2511 | 据え置き。上回る商用可のモデルは見つからなかった |
| 動画（音声付き） | LTX-2 → **LTX-2.5** | LTX-2.5 が 2026-08 に公開された。年商 1,000 万ドル未満は商用無料 |
| 動画（無音） | Wan2.2-I2V-A14B | 据え置き（Apache-2.0）。Wan 2.5 以降は重み非公開 |
| 読み上げ（声をまねる） | 未定 | **Qwen3-TTS-1.7B-Base**（Apache-2.0）に決定。次点 VoxCPM2 |
| 書き起こし | なし | Qwen3-ASR-1.7B（Apache-2.0、llama.cpp 公式 GGUF）が候補。lao にはまだ機能がない |
| 音楽・効果音 | なし | ACE-Step 1.5（MIT、出力の商用利用を明記）が候補。lao にはまだ機能がない |

## 会話・画像理解

| モデル | 大きさ | 画像 | ツール | ライセンス |
|---|---|---|---|---|
| Qwen3.6-35B-A3B（2026-04） | 35B・稼働 3B、Q8_0 36.9GB | ○ | ○ | Apache-2.0［確］ |
| Qwen3.8-27B（2026-08） | 密 27B | ○ | ○ | Apache-2.0［確］。GGUF 未確認 |
| gemma-4-31B-it（2026-07） | 密 31B、Q8 約 33GB | ○ | ○ | Apache-2.0［確］ |
| gemma-4-12B-it | 密 12B、公式 QAT GGUF | ○＋音声 | ○ | Apache-2.0［確］ |
| Mistral-Small-4-119B-2603（2026-03） | 119B・稼働 6.5B、IQ4_XS 約 63GB | ○ | ○ | Apache-2.0［確］。日本語を明記 |
| Nemotron-3.5-Lightning-30B-A3B（2026-08） | 30B・稼働 3B | × | ○ | OpenMDW 1.1、商用可と明記［確］ |
| LFM2.5-24B-A2B（2026-10） | 24B・稼働 2.45B | × | ○ | lfm1.0。年商 1,000 万ドル未満は商用可 |
| gpt-oss-120b | 117B・稼働 5B | × | ○ | Apache-2.0。後継は出ていない |

- 載らない大きさ：Qwen3.8-Flash-Next（180B）、GLM-5.3-Flash、DeepSeek-V4.1-Flash、MiniMax-M3.1-Flash など。
- 除外：Cohere Command A／Aya（非商用）。
- 動きのない系統：Llama 4 以降なし、Phi の新作なし、InternVL3.5 以降なし。

## 画像

| モデル | 大きさ | ライセンス | メモ |
|---|---|---|---|
| Qwen-Image-2512／Edit-2511／Layered | 20B | Apache-2.0 | 日本語の文字が最も書ける。Layered は透過素材に使える |
| FLUX.2 klein 4B（2026-01） | 4B | Apache-2.0［確］ | 速い下書き向け。ゲーム側のスキルで使用中 |
| Z-Image-Turbo／Z-Image（2025-11／2026-01） | 6B | Apache-2.0［確］ | 文字は中国語と英語のみ。Edit 版は未公開 |
| SenseNova U1.5（2026-09） | 18B | Apache-2.0［確］ | 4K、参照 10 枚の編集。Edit-2511 の対抗として試す価値あり |
| FLUX 3 Image Klein（2026-10） | 5.6B | 年商 500 万ドル未満なら商用可［確］ | 条件付き |
| Illustrious／NoobAI（SDXL のアニメ系） | — | Fair AI Public License［二］ | 出力は商用可。アニメ調のゲーム絵に強い |

- 除外：Qwen-Image-2.1（研究用）、FLUX.2 klein 9B／FLUX.2 dev／FLUX.1 dev（非商用）、Fibo 2.0（非商用）。
- 補助：拡大は SeedVR2（Apache-2.0）か Real-ESRGAN。4x-UltraSharp は非商用。背景除去は BiRefNet（MIT）。RMBG-2.0 は非商用。

## 動画

| モデル | ライセンス | メモ |
|---|---|---|
| LTX-2.5（2026-08、22B） | 年商 1,000 万ドル未満は商用無料［確］ | 4K、音声付き、ComfyUI 標準対応。AI 生成の表示が必要 |
| Wan2.2（I2V／T2V／S2V／Animate） | Apache-2.0 | ComfyUI 標準対応 |
| Kandinsky 6.0 Video（2026-10） | MIT［二］ | 音声・口の動き付き。ComfyUI は独自拡張 |
| InfiniteTalk | Apache-2.0［確］ | 音声に合わせて話す人物（口パク） |
| LongCat-Video-Avatar-1.5 | MIT［確］ | 二人の会話 |
| MiniMax H3（2026-08） | 条件付き［確］ | 日本語の台詞付き音声。ただし EU・英国・韓国・**米国では使えない** |
| HunyuanVideo-1.5 | 条件付き［確］ | EU・英国・韓国で使えない |

除外：Wan 2.5〜3.0（重み非公開）。

## 音声

| 役割 | 推し | 次点 | 除外（非商用） |
|---|---|---|---|
| 読み上げ・声まね | Qwen3-TTS-12Hz-1.7B-Base（Apache-2.0［確］） | VoxCPM2（Apache-2.0［確］） | Fish-Speech／OpenAudio、F5-TTS、MaskGCT、VibeVoice |
| 書き起こし | Qwen3-ASR-1.7B（Apache-2.0［確］） | kotoba-whisper-v2.2（Apache-2.0［確］） | — |
| 音声を聞く | Qwen3-Omni-30B-A3B（Apache-2.0［確］） | Gemma 4 E4B／12B（30 秒まで） | — |
| 音楽 | ACE-Step 1.5（MIT［確］） | YuE（日本語の歌詞） | MusicGen |
| 効果音 | Stable Audio Open（年商 100 万ドル未満［二］） | HunyuanVideo-Foley（地域条件あり） | MMAudio |

声をまねるのは、自分の声か許可を得た声だけにする。

## 実機で確かめること

- Qwen3.8-27B・Gemma 4 31B・Mistral Small 4 の GGUF と mmproj の配布元とファイル名。
- Qwen3-TTS を Windows on Arm で動かす方法。flash-attn などの arm64 版がない可能性がある。
- 二次情報のライセンス：Kandinsky 6、Stable Audio Open、YuE、Illustrious。
