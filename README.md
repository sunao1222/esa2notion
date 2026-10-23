# esa2notion

esaからエクスポートしたMarkdownのZIP、または展開済みディレクトリをNotionへ移行するPython CLIです。画像は元URLからダウンロードし、Notion File Upload APIへアップロードして本文中の画像を置換します。最終的にNotion管理のファイルであることとcaptionをAPIで読み戻して確認します。

## セットアップ

Nix（flakes有効）が必要です。Python 3.12とuvはNixから提供し、Python依存パッケージはuvで管理します。

```sh
nix develop
uv sync --locked
uv run --locked esa2notion --help
```

`uv sync --locked`が`.venv`を作成・同期するため、手動の`venv`作成や`pip install`、activateは不要です。Nixシェルは`UV_PYTHON`でNix提供のPythonを指定し、`UV_PYTHON_DOWNLOADS=never`でuvによるPythonの追加ダウンロードを禁止します。シェルに入るだけでは依存パッケージのインストールやネットワーク通信を行うhookは実行しません。

- `flake.nix`：Linux/macOSのx86_64・aarch64向け開発シェルとformatterを公開
- `shell.nix`：Python・uvと環境変数を定義（flakeから読み込む設定）
- `flake.lock`：nixpkgsを固定
- `uv.lock`：Python依存パッケージのバージョンとハッシュを固定

初回はNixの取得・ビルドとPythonパッケージ取得でネットワークを使用します。Git管理下ではNix設定と両lockfileをリポジトリに含めてください。まだGitに追加していないローカルファイルで試す場合は`nix develop path:.`を使用します。

依存を意図的に更新するときだけ`uv lock --upgrade`を使い、更新した`uv.lock`を共有します。以下の実行例はすべて`nix develop`内で実行してください。

## 1. 書き込みなしで入力を確認する

```sh
uv run --locked esa2notion /path/to/esa-export.zip --dry-run
# 展開済みディレクトリも指定可能
uv run --locked python -m esa2notion /path/to/esa-export --dry-run
```

記事ごとの相対パス、タイトル、カテゴリの配列、画像出現数、固有URL数、esa認証が必要かをJSON Linesで出力します。最後の行は全体の集計です。トークン・親ページIDは不要で、HTTP通信も画像保存も行いません。

`.md` / `.markdown` を再帰的に読み、相対パス順に処理します。UTF-8が必要です。ZIPは展開せず直接読みます。危険なパスやsymlinkのZIPメンバーは拒否し、ディレクトリ内のsymlinkと`__MACOSX`は読みません。

記事のYAML front matterは次のように解釈します。

```markdown
---
title: デプロイ手順
category: 開発/運用
tags: [運用, 手順]
created_at: 2024-01-02T03:04:05+09:00
updated_at: "2024-02-03T04:05:06Z"
published: false
number: 123
---
# 本文
![構成図](https://img.esa.io/your-image.png)
```

この例では、指定親ページ → 開発 → 運用 → デプロイ手順を作成します。`title`が欠落・null・空の場合はファイルstem、`category`が欠落・null・空なら親ページ直下です。ファイルパスからカテゴリは推測しません。

記事本文の先頭へ、元のYAML front matter全体（開始・終了の`---`行を含む）を`yaml`コードブロックとして追加し、その後に元本文を配置します。`title`、`category`、`tags`、`created_at`、`updated_at`、`published`、`number`だけでなく、未知のキー、コメント、キー順、引用、重複キーも元の表記で保持します。YAML内の画像記法は文字列のままで、ダウンロード対象にしません。Notion/Markdown側で改行がLFへ正規化される場合があります。

ヘッダーがなければ追加せず、明示的な空ヘッダーは保持します。カテゴリページには記事ヘッダーを追加しません。タグ・日時はYAMLでの表示のみで、Notionのタグプロパティや実際の作成・更新日時には設定しません。

## 2. Notionを準備する

1. 対象workspace用のNotion connectionを作成します。
2. Read Content / Insert Content / Update Contentを許可します。
3. **空の検証用親ページ**を用意し、connectionへ共有します。
4. 親ページのUUIDをURLから確認します。
5. connectionのトークンを`NOTION_TOKEN`環境変数へ設定します。トークンを引数やファイルへ保存する機能はありません。

移行後の共有範囲はNotionの親ページから継承されます。**esaの閲覧権限は複製しません。セキュア画像を含む記事では親ページの共有範囲を必ず確認してください。**

## 3. 移行する

`NOTION_TOKEN`を環境変数に設定したシェルから実行します。

```sh
uv run --locked esa2notion /path/to/esa-export.zip \
  --parent-page-id 12345678-1234-1234-1234-123456789abc
```

`files.esa.io` / `dl.esa.io`のセキュア添付が含まれる場合は、`ESA_TOKEN`も環境変数へ設定し、チーム名を渡します。

```sh
uv run --locked esa2notion /path/to/esa-export.zip \
  --parent-page-id 12345678-1234-1234-1234-123456789abc \
  --esa-team your-team
```

セキュア画像の署名URLはesa APIへ取得直前に要求します。esa以外も含め、通常のHTTP(S) GETで取得できる画像に対応し、リダイレクトを追跡します。公開画像には`ESA_TOKEN`・`--esa-team`は不要です。Notion/esaのAPI認証を画像GETへ送信しません。独自S3などのHTTP(S)画像ホストも登録不要ですが、独自ログイン・認証が必要なプロバイダには対応しません。

画像GETは`User-Agent: esa2notion`で送信します。requests標準のUser-Agentを拒否する画像CDNでもアプリ名で識別できるようにし、ブラウザには偽装しません。Imgurで標準User-Agentには429、アプリを識別するUser-Agentには200を返すケースを確認しています。実際のアクセス制限や削除・非公開画像を回避する機能ではありません。

作成したページURLを逐次表示します。取得できた画像をNotion管理ファイルとして確認し、取得に失敗した画像を「画像リンク切れ」へ置き換えた後に「記事完了」と表示します。取得失敗がある記事には、失敗した固有URL件数も付記します。

## 失敗時の扱い

**毎回、新規ページを作成します。既存記事の更新・skip・再開機能はありません。再実行はカテゴリと記事の重複を作ります。**

画像URLの取得に失敗しても、該当位置を**「画像リンク切れ」**に置き換えて後続の記事へ進みます。HTTPエラー、タイムアウト、空データ・非画像応答、対応外MIME、サイズ上限超過、esa署名URL取得失敗などが対象です。失敗画像のURLやaltは代替表示へ追加しません。表の失敗画像は元セル内に代替表示し、表の後へ重複配置しません。

失敗発生時にstderrへ`画像取得失敗: `とJSONを1行出力し、最後に`画像取得失敗一覧: M件`とJSON Linesで一覧を表示します。各行には`source`（元記事の相対パス）、`title`、`url`、`error`を含みます。件数は記事とURLの組み合わせ単位です。同じ記事での同一URLの重複は1件、別の記事に現れた場合は別の1件として記録します。URLのquery・fragment・埋め込み認証情報はログから除外します。元の完全なURLは記事ファイルで確認してください。

入力・設定の不備（不正YAML、相対画像URL、esaセキュア画像の資格情報欠落など）や、Notionへの作成・アップロード・画像置換・読み戻しの失敗では停止します。生のファイルI/Oエラーなども続行対象外です。停止時には失敗記事・作成済みURL・完了記事数をstderrに出し、それまでの画像取得失敗も一覧表示します。部分作成されたページは自動削除しません。既存のNotionページも変更・削除しません。画像置換途中の失敗ではmarker captionや一時的なexternal画像が残る場合があります。

**全記事を完走しても画像取得失敗があれば終了コード1です。非0を理由に自動で全件再実行しないでください。** 作成済みページと失敗一覧を確認してください。同じ親への再実行は重複を作ります。

429/529は`Retry-After`を優先して最大6試行します。Notionへのリクエストは逐次実行し最低0.35秒間隔です。タイムアウト・接続切断・その他の5xxでは、重複作成を避けるため書き込みを盲目的に再送しません。確定済みresource IDが応答にあれば、そのURLも通知します。

終了コード: `0`全画像成功・画像なし・dry-run成功、`1`画像取得失敗を含む完走、または解析・移行の致命的失敗、`2`引数不正。

## 対応範囲・制約

- Markdown画像、参照形式画像、HTML `<img>`を処理します。コード内の画像記法は処理しません。
- 画像はHTTP(S)絶対URLが必要です。相対パス、data URL、src欠落はエラーです。サイズ指定（width/height）は再現しません。
- 表セル内の取得成功画像は元altをセルへ残し、表の直後へ画像を配置します。captionに元の行・列を付けます。取得失敗画像はセル内で「画像リンク切れ」に置き換えます。リスト・引用内画像と代替表示は子ブロックとして出力します。
- 見出し・コード・リスト・タスク・打消し線・表をMarkdownで渡します。HTML全体の変換器ではなく、Notionが対応しないHTMLやesa独自記法の完全再現は保証しません。
- 画像形式はNotion対応MIMEに限定し、画像変換はしません。ログインHTML・空データ・非画像などの取得失敗は代替表示と失敗一覧へ記録します。
- 画像上限はNotion botのworkspace limitsから取得します。Freeは5 MiB、Paidは5 GiBが公式上限です。20 MiB超は10 MiB単位のmultipart uploadを使います。
- 同じ完全URLの取得結果は成功・失敗とも実行中に共有し、別記事でも再取得しません（HTTP層のretryは別）。queryが異なるURLは別扱いです。同じ記事内ではupload IDも再利用します。ダウンロードした画像は実行終了時に一時ディレクトリごと削除します。
- Notion本文APIのリクエストサイズ・ブロック数制限は回避しません。超過は明示的な失敗であり、本文を切り捨てたり別記事へ分割したりしません。
- 元のesa記事へのリンクは変更しません。履歴・コメント・投稿者のNotion属性への移行、タグプロパティや実際の作成・更新日時の設定は対象外です。元YAMLに含まれる情報は本文先頭のコードブロック内に保持します。

Notion APIのバージョンは`2026-03-11`、Markdownバージョンは`v1`を使用します。本文作成時には元URLと一意marker captionを持つ一時external画像を渡し、画像ブロックを再帰取得してNotion uploadへ置換します。markerの欠落・重複は誤置換せず停止します。

## 検証

```sh
uv run --locked python -m unittest discover -s tests -v
```

Nix設定の評価は`nix flake check`、Nixファイルの整形は`nix fmt -- flake.nix shell.nix`を使います。formatterは固定したnixpkgsの`nixfmt`（旧`nixfmt-rfc-style`）です。

`x86_64-linux`で`nix flake check path:.`、`nix develop path:. --command uv sync --locked`、uv経由のCLI help・サンプル記事dry-runの成功を確認しています。今回の変更はNix+uv経由で全55回帰テストを実行しました。ほかのOS/アーキテクチャは設定を公開していますが、実行は未検証です。

回帰テストは入力の安全性、生YAML（コメント・重複キー・日時・CRLF）の保持、安全なコードfence、Markdown/コードの保持、画像取得の上限、20 MiB境界、multipart最終part、再試行、画像置換を確認します。取得失敗時の本文順序・リスト/引用・表セルの代替表示、成功/失敗キャッシュ、記事URL単位の集計、URL秘密情報の非表示、後続Notion失敗時の停止と一覧保持も確認します。公開HTTP画像の直接取得と別originへの302、両originへのAPIヘッダー非送信は実loopback HTTPで検証します。

一時HTTPハーネスから実CLIを別プロセスで起動し、ZIP/ディレクトリのdry-run（3記事・6画像箇所・3固有URL、HTTPゼロ）と実移行を確認しました。404画像があっても3記事を完走し、4箇所の「画像リンク切れ」、先頭YAMLと本文順序、正常2画像の管理file化・caption・upload PNGバイト一致、404へのGETが1回だけ、記事URL別の警告・最後の2件一覧、終了コード1を確認しました。全成功時は終了コード0で警告なし、Notion uploadのHTTP400では後続記事へ進まず終了コード1となり、既知の取得失敗一覧も残ることを確認しました。

Imgurの`https://i.imgur.com/CkSd9EP.jpg`は、修正後の実downloaderでJPEG 2,021バイトとして取得できることを確認しました。標準requestsのUser-Agentを拒否するloopbackサーバーで、修正前は失敗し、修正後は直接取得・別originへのredirectとも成功する回帰テストも実行済みです。

**検証用の空のNotion親ページが未指定のため、実サービスのMarkdown受理・YAML言語表示・画像生成・marker保持・権限・「画像リンク切れ」の画面表示は未検証です。** まず少数記事を空の検証用親ページへ移行し、Notionの画面上で階層・先頭YAML・本文・画像を確認してから本番の移行先を決めてください。

API仕様:
- [esa Markdown export](https://docs.esa.io/posts/11)
- [Notion Markdown](https://developers.notion.com/guides/data-apis/working-with-markdown-content)
- [Notion対応ファイル形式・上限](https://developers.notion.com/guides/data-apis/working-with-files-and-media)
- [Notion File Upload](https://developers.notion.com/guides/data-apis/uploading-small-files)
