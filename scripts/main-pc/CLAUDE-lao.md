## Spark（lao）の使い方
<!-- lao-block v2（scripts\prepare-main-pc.ps1 が書き換える。手で直すときはこの印を消す） -->
Spark は LAN 内の RTX Spark で、lao（MCP サーバー lao）が画像・動画・音声の生成と下請けをする。利用者は Claude Code と lao の画面だけを使うので、Spark への操作は lao の道具と下の spark.ps1 で Claude が行う。

- 任せる順番の決まり：Claude にできないこと（画像・動画・読み上げ音声の生成、音声・動画の聞き取り）は必ず lao に任せる。次に、Claude のトークンを多く使う作業（長い文書の要約・翻訳・下書き、画像用の英語プロンプト作り）を lao の ask_local に任せる
- メイン PC のファイルを lao に読ませるときは、自分で中身を読まずに `powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 put <パス>` で送り、表示された名前を ask_local の inbox（要約など）か import_file（画像・動画の素材）に渡す
- 結果は ref と Spark 上の保存場所だけ受け取る。利用者が手元に欲しいときだけ `spark.ps1 get "<保存場所>"` で ~/Downloads/lao に取ってくる
- 作った画像・動画の確認は、まず describe_media に任せる。自分で見るのは view で、必要なときだけ（案を比べるときは generate_image の count で作り、view の refs で並べた 1 枚を見る）
- 決まった絵柄は save_style で保存し、generate_image の style で呼ぶ。動画に読み上げを付けるなら make_clip
- lao の道具がつながらないときは `spark.ps1 check`、眠っていそうなら `spark.ps1 wake`。利用者に ssh や scp を打たせない
- 利用者が「lao の画面を開いて」と言ったら `spark.ps1 open`
- Spark の手入れ：health（容量・温度）、cleanup（既定は一覧だけ）、update（既定は確認だけ）、backup
