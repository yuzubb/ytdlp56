# ytdlp_api (UIなし・API専用サーバー ※/api/statsのみHTML UIあり)

yt-dlp を使った軽量API。Flask + requests のみで構成(fastapi/pydanticは不使用)。

> Termux (Python 3.14 / aarch64-linux-android) では `fastapi` の依存である `pydantic-core` の
> ビルドに Rust(maturin)が必要になりますが、rustupがこのターゲットを未サポートのため
> 実質インストールできません。この構成ではFlaskを使うことでRustのビルドを完全に回避しています。

## エンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api` | **API一覧・説明・実行テストページ(HTML)**。各エンドポイントに入力欄と「実行」ボタンがあり、その場で叩いて結果を確認できる |
| GET | `/api/health` | 死活監視 |
| GET | `/api/search` | YouTube検索。`?q=検索語&limit=件数`。7時間キャッシュ |
| GET | `/api/suggest` | 検索窓の入力補完(サジェスト)。`?q=入力途中の文字列` |
| GET | `/api/token/issue` | 署名付き公開トークンを発行(有効期限1日) |
| GET | `/api/token/verify` | トークンの有効性を検証。`?token=...` |
| GET | `/api/visit` | サイト累計閲覧数を取得(増やさない) |
| POST | `/api/visit` | サイト累計閲覧数を1増やして取得 |
| GET | `/api/history` | みんなの視聴履歴を取得(個人には紐づかない)。`?limit=...` |
| GET | `/api/user/me` | 自分のプロフィール(表示名・ユーザーID・アイコン)を取得(要ログイン) |
| PUT | `/api/user/me` | プロフィールを更新(要ログイン) |
| POST | `/api/inquiries` | お問い合わせを送信(要ログイン) |
| GET | `/api/inquiries` | お問い合わせ一覧。一般ユーザーは自分の分だけ、オーナーは全員分 |
| GET | `/api/inquiries/{id}` | お問い合わせ詳細+返信一覧(本人かオーナーのみ) |
| POST | `/api/inquiries/{id}/replies` | 返信を送信(本人かオーナーのみ) |
| DELETE | `/api/inquiries/{id}` | お問い合わせを削除(オーナーのみ、403で保護) |
| GET | `/api/playlists` | 自分のプレイリスト一覧(要ログイン) |
| POST | `/api/playlists` | プレイリストを新規作成(上限10個、名前は20文字まで) |
| GET | `/api/playlists/{id}` | プレイリストの詳細+動画一覧(本人のみ) |
| PUT | `/api/playlists/{id}` | プレイリスト名を変更 |
| DELETE | `/api/playlists/{id}` | プレイリストを削除 |
| POST | `/api/playlists/{id}/videos` | 動画を追加(上限100本) |
| DELETE | `/api/playlists/{id}/videos/{video_id}` | 動画を削除 |
| GET | `/api/admin/banned-words` | NGワード一覧(オーナーのみ) |
| POST | `/api/admin/banned-words` | NGワードを1つ追加(オーナーのみ) |
| POST | `/api/admin/banned-words/bulk` | NGワードをまとめて追加(オーナーのみ) |
| POST | `/api/admin/banned-words/import-url` | 外部URLからNGワードを一括インポート(オーナーのみ) |
| DELETE | `/api/admin/banned-words/{id}` | NGワードを削除(オーナーのみ) |
| GET | `/api/admin/banned-ips` | BAN中のIP一覧(オーナーのみ) |
| POST | `/api/admin/banned-ips` | IPを手動BAN(オーナーのみ) |
| DELETE | `/api/admin/banned-ips/{ip}` | BAN解除(オーナーのみ) |
| GET | `/api/admin/ban-events` | BANログ。`?ip=...`または`?email=...`で絞り込み(オーナーのみ) |
| DELETE | `/api/admin/banned-words/by-text` | 単語を指定して削除(一覧を見せない運用のため、オーナーのみ) |
| DELETE | `/api/admin/banned-words` | NGワードを全て削除(オーナーのみ) |
| GET | `/api/admin/banned-emails` | BAN中のメールアドレス一覧(オーナーのみ) |
| POST | `/api/admin/banned-emails` | メールアドレスを手動BAN(オーナーのみ) |
| DELETE | `/api/admin/banned-emails/{email}` | メールアドレスのBAN解除(オーナーのみ) |
| GET | `/api/admin/moderation-policy` | AI判定基準・有効状態を取得(オーナーのみ) |
| PUT | `/api/admin/moderation-policy` | AI判定基準を更新(オーナーのみ) |
| POST | `/api/moderation/check-search` | 検索前のNGワードチェック(ログイン不要) |
| GET | `/api/ban/check` | 自分(リクエスト元IP)のBAN状態を確認(ログイン不要) |
| POST | `/api/history` | みんなの視聴履歴に1件記録(同じ動画の再視聴は日時だけ更新) |
| DELETE | `/api/history` | みんなの視聴履歴を全削除(要管理者パスワード) |
| GET | `/api/playlist/{playlist_id}` | プレイリストのメタ情報+収録動画一覧。`?limit=&offset=`で範囲指定。7時間キャッシュ |
| GET | `/api/channel/{channel_id}` | チャンネルのメタ情報+投稿一覧+アバター/バナー(base64)。`?tab=videos\|streams\|shorts\|playlists`でタブ切り替え。`@handle`/`UCxxxx`/フルURLいずれも可。7時間キャッシュ |
| GET | `/api/comments/{video_id}` | 動画のコメント一覧。`?limit=`で件数指定。7時間キャッシュ |
| GET | `/api/related/{video_id}` | 関連動画。動画ページの`ytInitialData`を解析して取得(非公式、後述) |
| GET | `/api/trending` | トレンドフィード。`?category=trending\|music\|gaming`。外部ソースを1時間キャッシュ |
| GET | `/api/info/{video_id}` | 動画の全メタデータ(ストリームURLは含まない) |
| GET | `/api/stream/{video_id}` | ストリームURL一覧+動画詳細(info相当)。HLS(m3u8)直リンクがあれば`hls_url`に入る |
| GET | `/api/proxy-stream/{video_id}` | 実際の映像/音声バイト列をサーバー経由で中継する(後述、`?format_id=`で指定) |
| GET | `/api/livechat/{video_id}` | ライブ配信のチャット取得(試験的機能、後述) |
| GET | `/api/stats` | **HTMLダッシュボード**。worker/処理中/キャッシュ件数/稼働時間を2秒おきに自動更新表示 |
| GET | `/api/stats/data` | ↑と同じ内容をJSONで返す(自作クライアント用) |
| GET | `/api/workers` | このサーバー(worker)の情報 |
| GET | `/api/processing` | 現在処理中のvideo_id一覧 |
| GET | `/api/cache` | これまでに解決した動画の一覧(`?q=`検索、`?limit=`、`?offset=`) |
| GET | `/api/cache/{video_id}` | キャッシュ済み単一動画の情報 |
| DELETE | `/api/cache/{video_id}` | 一覧インデックス+レスポンスキャッシュから削除(`?password=`必須) |
| DELETE | `/api/cache` | キャッシュを全部削除(強制リフレッシュ用、`?password=`必須) |

### video_id について

- YouTubeなら動画ID単体でOK: `/api/info/dQw4w9WgXcQ`
- YouTube以外や別のURLを使いたい場合は、URLをそのままURLエンコードして渡す:
  `/api/info/https%3A%2F%2Fvimeo.com%2F123456789`

## レスポンスキャッシュ(7時間)

`/api/info` と `/api/stream` は、同じ`video_id`に対する結果を**7時間**保存します(`YTDLP_API_CACHE_TTL_SECONDS`で変更可、デフォルト `7*3600`秒)。
期間内に同じvideo_idへ再リクエストが来た場合、yt-dlpを一切呼ばずキャッシュを即座に返します。

レスポンスの `_cache` フィールドで確認できます。

```json
{
  "_cache": { "hit": true, "age_seconds": 120.4, "expires_in_seconds": 24879.6 }
}
```

> **注意**: CDN側の直リンク(googlevideo等)はYouTube側の都合で数時間程度で失効することがあります。
> 7時間キャッシュの後半で `/api/stream` のURLが再生できない場合は、`DELETE /api/cache/{video_id}` で
> 一覧インデックスを消してから再度リクエストすると新しいURLが取得できます
> (レスポンスキャッシュ自体は自然失効を待つか、サーバー再起動で消えます)。

## `/api/search`, `/api/playlist`, `/api/channel` について

`/api/search`と`/api/playlist`はYouTubeのページを直接スクレイピングしています
(related/trendingと同じ「動画カードを総当たりで拾う」方式)。この方式だと各結果に
投稿者の小さいアイコン画像(`channel_thumbnail`)も付いてきます(プレイリストは収録動画の
投稿者がバラバラなことがあるため、動画ごとに個別のアイコンを持たせています)。
スクレイピングが失敗した場合のみ、yt-dlpのflat抽出にフォールバックします
(この場合`channel_thumbnail`は付きません)。

`/api/channel`の一覧は引き続きyt-dlpの`extract_flat`で取得していますが、
同じページの動画は全部同じチャンネルのものなので、チャンネル自身のアバターを
`channel_thumbnail`として各エントリに埋め込んでいます。

### 無限スクロール(続きのページ取得)

- `/api/search`: `?continuation=`にレスポンスの`next_continuation`を渡すと次のページが
  取れます。YouTube本家の内部API(youtubei)を直接叩いているので、実際に追加の
  検索結果を取得できます(単なる同じ結果の水増しではありません)。
  - ページ内には`ytcfg.set({...})`が複数回出てくることがあり、1回目の呼び出しだけでは
    `INNERTUBE_API_KEY`/`INNERTUBE_CONTEXT`が揃っていないことがあったため、
    見つかった`ytcfg.set`を全部マージしてから必要な値を取り出すようにしています。
  - continuation取得リクエストにも`Accept-Encoding`を付けている都合上、gzip圧縮された
    レスポンスが返ってくることがあるため、`_fetch_page`と同様に手動展開しています。
  - `INNERTUBE_API_KEY`がページから全く取れなかった場合でも、YouTubeの一般公開Web
    クライアントが共通で使っている既知のキー(`_FALLBACK_INNERTUBE_API_KEY`)に
    フォールバックするので、502で完全に失敗することは無くなっています
    (`INNERTUBE_CONTEXT`も同様に最低限のダミー値にフォールバックします)。
- `/api/channel`: `?offset=`を増やしていくだけで続きが取れます(yt-dlpが内部で
  必要なページ送りを面倒見てくれるため、追加の仕組みは不要でした)。
- `/api/playlist`もスクレイピング成功時は`offset`で範囲を絞れます。

```bash
curl "https://xxxx.ngrok-free.app/api/search?q=猫&limit=10"
curl "https://xxxx.ngrok-free.app/api/playlist/PLxxxxxxxxxxxx?limit=50"
curl "https://xxxx.ngrok-free.app/api/channel/@handle?limit=30&tab=shorts"
```

## `/api/comments/{video_id}` について

yt-dlpの`getcomments`機能を使っています。`?limit=`で取得件数の目安を指定できますが、
動画のコメント数やYouTube側の状況によっては指定件数より少なく返ることがあります。
件数が多いと処理に時間がかかるので、`limit`は必要な分だけに抑えるのがおすすめです。

## `/api/related/{video_id}` について

yt-dlp自体には関連動画を取る機能が無いので、`requests`(コネクション使い回し用にSession化済み)で
watch pageのHTMLを取得し、埋め込まれている`ytInitialData`から関連動画を抜き出しています。

決め打ちのJSONパス(`contents.twoColumnWatchNextResults.secondaryResults...`)には依存していません。
代わりに、JSON全体を再帰的に舐めて「videoId + title を持ち、thumbnailやlengthTextなど
動画カードらしい特徴もある」ノードを片っ端から拾う方式にしています。そのため、YouTubeが
階層構造やレンダラー名(`compactVideoRenderer`など)を変えてきても、動画カード自体の
基本的な形が大きく変わらない限りは自動的に追従します。

```json
{
  "video_id": "abc123",
  "method": "youtube_watch_page_scrape",
  "entry_count": 10,
  "entries": [
    {"video_id": "...", "title": "...", "channel": "...", "channel_id": "...",
     "length_text": "10:23", "view_count_text": "12万 回視聴", "thumbnail": "...", "url": "..."}
  ]
}
```

それでも「動画カードの形」自体が根本的に変わるようなリニューアルがあれば流石に追従できないので、
その場合は`502`エラー(`"failed to parse ytInitialData..."`)が返ります。



ストリームURL(`formats`, `url`, `manifest_url`, 字幕ファイルURLなど)を除いた、
yt-dlpが取得できる**ほぼ全てのメタデータ**をそのまま返します。実際には動画によって

- 基本情報: タイトル、動画ID、説明文、アップロード日、チャンネル名/ID、URL
- 統計データ: 再生回数、高評価数、コメント数
- 構造化データ: チャプター(タイムスタンプ+タイトル)、タグ、カテゴリ
- その他: ライブ配信状態、公開設定、シリーズ/音楽メタデータなど

を含む **100以上のフィールド** が返ります。解像度/FPS/ファイルサイズだけは
「参考値」として `resolution_reference` にまとめてあります(該当フォーマットのURL自体は含みません)。

## `/api/stream/{video_id}` の内容

```json
{
  "video_id": "abc123",
  "title": "...",
  "is_live": false,
  "streams": [
    {"format_id": "137", "ext": "mp4", "width": 1920, "height": 1080, "fps": 30, "vcodec": "avc1...", "acodec": "none", "url": "https://...", "protocol": "https", "filesize": 12345678, ...},
    {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 128, "url": "https://...", ...}
  ],
  "hls_url": "https://.../master.m3u8",
  "proxy_url_template": "/api/proxy-stream/abc123?format_id={format_id}",
  "cache_ttl_seconds": 25200
}
```

- `streams` にその動画で利用可能な**全フォーマット**の直リンクが並びます(映像/音声別、解像度別など)。
- `hls_url` はyt-dlpが把握しているネイティブHLS(m3u8)直リンク(YouTubeのライブ配信等で存在)。VODなど無い場合は`null`。
- `streams[].url` は**ブラウザから直接叩くと再生できないことがあります**(後述)。基本的には
  `proxy_url_template` の `{format_id}` を埋めた `/api/proxy-stream/{video_id}?format_id=...` を
  使うことを想定しています。

## `/api/proxy-stream/{video_id}` について(重要)

yt-dlpが解決するCDN直リンク(`streams[].url`)は、**それを解決したサーバーのIPアドレスからの
アクセスしか受け付けない**ことがあります。つまりサーバー側(Termux)では`streams[].url`が
正しく取得できていても、そのURLをブラウザ(利用者の端末のIP)から直接叩くと再生できない
ことがあります。これが「普通に再生もできない」という不具合の主な原因でした。

`/api/proxy-stream/{video_id}?format_id=18` は、サーバー自身がCDNからバイト列を取得して
そのままクライアントに中継します。取得元がサーバーのIPのままになるので、この問題を回避できます。
Rangeリクエストにも対応しているのでシークも普通に効きます。URLが失効していた場合は
1回だけ自動で再解決してリトライします。

## `/api/livechat/{video_id}` について(試験的機能)

ライブ配信のチャットを取得します。**継続トークンを辿って配信全体のチャット履歴を遡る
本格的な実装ではなく**、yt-dlpが教えてくれるチャットデータの最初のURLに一度アクセスして、
そこに含まれているメッセージだけをパースして返す簡易版です。ライブチャットが存在しない
動画(通常のアップロード動画等)では`404`になります。

## `/api/trending` について

YouTube公式のトレンドページのスクレイピングは不安定だったため廃止し、代わりに
**このAPIを経由して実際に視聴された動画の集計**を返す方式に変更しました。
`/api/info/{video_id}`が呼ばれるたび(レスポンスキャッシュのヒット時も含む)に
`trending_data/views.json`へ視聴回数を記録し、`/api/trending`はその回数が多い順に
返します。まだ誰も見ていない状態では`entries`が空になります。YouTube本家のトレンドとは
無関係な、あくまで「このサイトでよく見られている動画」というローカルな集計です。

## トレンド(`/api/trending`)について

以前は「このAPI経由で実際に視聴された動画」を集計する自前方式でしたが、
誰も見ていないうちは空っぽになってしまう問題があったため、外部ソース
([siawaseok3/wakame](https://github.com/siawaseok3/wakame)の`trend.json`、1時間おき更新)を
使う方式に切り替えました。

- `?category=trending`(総合・既定)/ `music`(音楽) / `gaming`(ゲーム) を切り替えられます。
- サーバー側で**1時間キャッシュ**します(`WAKAME_TREND_CACHE_TTL_SEC`)。ソース側の
  更新頻度と合わせています。
- サムネイル・チャンネルアイコンは元URLのままだとホットリンクで壊れることがあるため、
  取得してbase64のdata URIに変換してから返します。チャンネルアイコンも動画サムネイルと
  同様に必ず変換しているので、「?」のまま出ることはありません。
- 変換対象の画像は3カテゴリ合計で100件近くになることがあるため、直列だと遅くなります。
  `concurrent.futures.ThreadPoolExecutor`で並列変換することで、初回(キャッシュが
  切れた直後)のリクエストでも実用的な速度に収めています。
- レスポンスの`updated`フィールドに元データの最終更新日時が入っているので、
  フロントエンド側で「N分前に更新」のような表示に使えます。

## ドメインのルート(`/`)について: 表向きは「ちょいツール」(実用ツール集)

このAPIは元々「表向きのUIが無いAPI専用サーバー」として作っていましたが、ドメインの
ルート(`/`)に直接アクセスした人にAPIの中身(yt-dlp云々)が見えてしまうのを避けるため、
実際に使える無料のツール集を「表の顔」として用意してあります
(ブログ・掲示板と試行錯誤しましたが、実際に人が使いたくなる実用ツールの方が
自然かつ保守も楽という判断です)。実際のAPI機能(`/api/*`)には一切影響しません。

収録ツール:

- **QRコード生成**(テキスト/URL、Wi-Fi接続情報)
- **文字数カウント**(文字数/空白除く/行数/単語数)
- **パスワード生成**(`crypto.getRandomValues`を使った暗号学的に安全な乱数)
- **単位変換**(長さ/重さ/温度)
- **カラーコード変換**(HEX/RGB/HSL相互変換、カラーピッカー付き)
- **Base64 / URLエンコード・デコード**
- **JSON整形**(整形/1行圧縮、壊れている場合はエラー内容を表示)
- **タイマー / ストップウォッチ**
- **消費税計算**(税込み/税抜き相互計算)
- **BMI計算**(日本肥満学会の基準で判定)
- **割り勘計算**(端数の切り上げ/切り捨て/四捨五入を選択可能)
- **乱数 / サイコロ / くじ引き**(`crypto.getRandomValues`使用)
- **文字列ケース変換**(UPPERCASE/lowercase/Title Case/camelCase/snake_case/kebab-case)
- **ハッシュ生成**(SHA-1/SHA-256/SHA-512、`crypto.subtle`使用)
- **トークン発行**(このサイト固有の署名付きトークン。有効期限1日、検証機能付き)

### トークン発行について(一般公開用のAPI認証)

他のツールと違い、これだけはサーバー側の処理が必要です(`/api/token/issue` /
`/api/token/verify`)。サーバーの秘密鍵(`YTDLP_API_SESSION_SECRET`)で署名した
トークンを発行するので、見た目はただの文字列でも、このサーバーでしか発行・検証
できません(秘密鍵を知らない第三者が同じ形式のトークンを偽造することはできない)。
発行から24時間で自動的に無効になります。サーバー側では何も保存していない
(ステートレスな)方式なので、DBの肥大化やクリーンアップの心配もありません。

**このトークンは飾りではなく、実際に`/api/*`全体(このドキュメントページ`/api`自身、
`/api/stats`も含む)への唯一のアクセス手段になっています。** `/api/token/issue`・
`/api/token/verify`以外の`/api/*`配下は、有効なトークンが無いと**404**を返します
(401/403ではなくあえて404にすることで、トークンが無い人には「そもそも何も無い」
ように見せています)。トークンは`X-API-Token`ヘッダー、または`?token=`クエリ
パラメータのどちらかで渡してください。一般の利用者が使うことを想定した経路です。

### `ytdlp_frontend`専用のバイパス(こちらが本命)

`ytdlp_frontend`は、1日で失効する公開トークンを毎回取りに行く代わりに、
**専用の合言葉(`YTDLP_API_FRONTEND_SECRET`)を`X-Frontend-Secret`ヘッダーで
送ることで、上記のトークンチェックを丸ごとスキップできます。** 一般の人はこの値を
知りようがないので、公開トークン方式の安全性には影響しません。

- 未設定なら起動時に自動生成され、`frontend_secret.txt`に保存されます
  (`admin_password.txt`等と同じ仕組み)。
- `ytdlp_frontend`側の環境変数`YTDLP_API_FRONTEND_SECRET`に、**バックエンドと
  全く同じ値**を設定してください。`frontend_secret.txt`の中身をそのままコピーすれば
  OKです。
- この値が一致していれば、フロントエンドは有効期限やトークン再取得を一切気にせず
  `/api/*`を素通りできます。値が一致しない/未設定の場合は、公開トークン方式に
  フォールバックします(ただしフロントエンド側がトークンを自動取得する仕組みは
  現在実装していないため、実質的にはこの合言葉の設定が必須です)。

### 広告バナーについて

ヘッダー直下に広告バナーを1件設置しています。内容は`templates/landing.html`の
`.ad-banner`要素を直接編集すれば差し替えられます(タイトル・説明文・リンク先の3点)。

- サーバー側には何も送信されません。全てブラウザ内のJavaScriptだけで完結しています
  (QRコード生成部分だけ`qrcodejs`をcdnjs経由で読み込んでいます)。DB・投稿フォーム・
  Bot対策は一切不要です。
- `/` が唯一のページで、左側(スマホでは上部)のタブでツールを切り替えます。
- テンプレートは `templates/landing.html` の1枚だけです。ツールを追加したい場合は
  同じファイル内に`.tool-card`セクションと`#toolNav`のボタンを追加するだけで済みます。

## cookies.txtについて(認証まわりはこちらが基本方針)

Bot判定によるブロック(`Sign in to confirm you're not a bot`等)を回避する方法として、
**`cookies.txt`を置く方式をメインの想定にしています**(後述のPO Token方式は
専用サーバーを常時起動し続ける必要があって運用が面倒だったため、
cookies.txtの方をおすすめします)。`cookies.txt`(Netscape形式)を置くと
yt-dlp・生スクレイピング(related/trending/channel等)の両方で自動的に使われます。

`cookies.txt`が使われている場合、`server.py`は`player_client`をあえて固定していません
(以前はPO Token方式向けに`mweb`等へ固定していましたが、ログイン済みcookieがあれば
yt-dlp標準のクライアント選択で通常問題なく通ります)。

**設置方法**: `server.py`と同じディレクトリに`cookies.txt`という名前で置くだけです
(検出したら自動で使われます。別の場所に置きたい場合は環境変数`YTDLP_API_COOKIES_FILE`で
パスを指定してください)。

### 複数アカウントのcookies.txtに対応(1つがダメでも次を自動的に試す)

1つのアカウントのcookieだけだと、bot判定に引っかかったりフォーマット取得に失敗した時に
打つ手がありません。そこで、**複数のcookies.txtを用意しておくと、1つ目が失敗したら
自動的に2つ目、3つ目…と順番に試す**ようにしています。

- `server.py`と同じディレクトリに `cookies.txt`, `cookies2.txt`, `cookies3.txt`
  ... と連番で置くだけです(存在するものを自動検出します。歯抜けは不可、
  `cookies.txt`→`cookies2.txt`→`cookies3.txt`と連続している必要があります)。
- 環境変数`YTDLP_API_COOKIES_FILE`にカンマ区切りで複数パスを指定することもできます
  (例: `YTDLP_API_COOKIES_FILE=/path/a.txt,/path/b.txt,/path/c.txt`)。この場合は
  連番自動検出より優先されます。
- 「次のcookieに切り替える」条件は、bot判定エラー・フォーマット取得失敗エラーです。
  ネットワーク起因のエラー(VPN切断等)の場合は、同じcookieのまま自動リトライします
  (別アカウントに切り替えても意味が無いため)。
- 全部のcookieで失敗した場合は、最後に試したエラー内容がそのまま返ります。

```bash
YTDLP_API_COOKIES_FILE=/path/to/cookies.txt python3 server.py
```

**cookies.txtの作り方**: ブラウザの拡張機能(例: "Get cookies.txt LOCALLY"など)を使って、
**実際にYouTubeへログインした状態**でcookieをNetscape形式でエクスポートしてください。
`SID` / `HSID` / `SSID` / `APISID` / `SAPISID` / `LOGIN_INFO`のようなcookieが
含まれていない場合、ログインが反映されていない可能性があります。

**重要な注意**:
- `cookies.txt`にはあなたのYouTubeログインセッションが入っています。**絶対に他人と共有したり、
  Gitにコミットしたりしないでください**(`.gitignore`で既に除外されるようにしてあります)。
- サーバー起動後に`cookies.txt`の中身を書き換えても、yt-dlp側は毎回ファイルを読み直すので
  即座に反映されますが、`_http`(requests)セッションの方は起動時に一度だけ読み込む仕組みに
  なっているため、反映させたい場合はサーバーの再起動が必要です。
- ファイルが存在しない場合は何も変わらず、これまで通りcookie無しで動作します。

## (予備の選択肢) ブラウザ拡張機能が使えない場合: PO Token方式

`cookies.txt`エクスポート用の拡張機能を入れられない場合、代わりに **PO Token**
(Proof of Origin Token)という仕組みを使う方法があります。ログイン情報(パスワード)を
一切扱わずに、YouTubeへ「正規のクライアントからのアクセスです」と証明するトークンだけを
生成する仕組みです。

**ただし実際に運用してみると、この方式専用のトークン生成サーバー(Node.js)を
常時起動し続ける必要があり手間がかかります。** 可能なら上記のcookies.txt方式を
優先することをおすすめします。この節は「拡張機能がどうしても使えない場合」の
参考情報として残しています。`server.py`側は現在この方式向けの`player_client`固定は
行っていないので、PO Tokenだけに頼る場合は自分で
`--extractor-args "youtube:player_client=mweb,web"`のような指定を追加で検討してください。


一切扱わずに、YouTubeへ「正規のクライアントからのアクセスです」と証明するトークンだけを
生成する仕組みで、yt-dlp公式が推奨している方法です。

**注意**: これはGoogleアカウントへの自動ログインではありません。パスワードを保存したり
入力したりする必要は一切なく、Bot判定を緩和するためのトークンを別プロセスで生成するだけです。

以下は `bgutil-ytdlp-pot-provider` というyt-dlp公式おすすめのプロバイダをTermuxで
セットアップする手順です。`ytdlp_api`本体のコード変更は不要で、これを別プロセスとして
起動しておくだけでyt-dlpが自動的に見つけて使ってくれます。

### 1. Node.jsを用意

```bash
pkg install nodejs git
```

### 2. Termux特有の依存関係の下準備

`canvas`というパッケージのビルドがTermuxだとそのままでは失敗するため、先にこれを実行しておきます。

```bash
mkdir -p ~/.gyp && echo "{'variables':{'android_ndk_path':''}}" > ~/.gyp/include.gypi
pkg install libvips xorgproto
```

### 3. トークン生成サーバーをセットアップ

```bash
git clone --single-branch --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
cd bgutil-ytdlp-pot-provider/server/
npm ci
npx tsc
```

### 4. yt-dlp側のプラグインをインストール

```bash
pip install -U bgutil-ytdlp-pot-provider --break-system-packages
```

### 5. トークン生成サーバーを起動

`ytdlp_api`本体(`python3 server.py`)とは別プロセスとして、常時起動しておく必要があります。
`tmux`で別ウィンドウを開いて実行するのがおすすめです。

```bash
cd ~/bgutil-ytdlp-pot-provider/server/
node build/main.js
# デフォルトで 127.0.0.1:4416 で待受。ytdlp_api側の設定変更は不要。
```

これでyt-dlpを使う`ytdlp_api`の各エンドポイントが、自動的にこのローカルサーバーから
PO Tokenを取得して使うようになります。

### PO Tokenだけでは不十分だった点(実機検証済み)

実際にTermux上で検証したところ、PO Tokenサーバーを立てるだけでは
`Sign in to confirm you're not a bot` エラーが解消しないケースがありました。
原因は2つあり、`server.py`側は既にこの3点をまとめて設定済みです(自分で`yt-dlp`を
CLIから直接叩いて試す場合は、以下を手動で指定してください)。

1. **PO Tokenに対応したクライアントを明示指定する必要がある**
   `android_vr`など一部のクライアントはPO Tokenがあってもログイン必須扱いになる。
   `mweb`クライアントを明示指定すると解決した。ただし**mweb単体だと動画によっては
   `itag 18`(既定の360p)などのフォーマットが欠けて`Requested format is not available`
   になることがあった**ため、`server.py`では`web`クライアントも一緒に指定して
   フォーマットの取得元を広げている(`web`側がBot判定で失敗しても、`mweb`側の結果が
   あるので全体としては失敗しない)。
   ```
   --extractor-args "youtube:player_client=mweb,web"
   ```
2. **署名解読用に外部JSランタイムが必要**(2025年後半以降のYouTube仕様変更)
   Termuxには既定ランタイムの`deno`が無いため、`node`を明示的に有効化し、
   解読スクリプト本体(EJS)をGitHubから取得する許可も必要。
   ```
   --js-runtimes node --remote-components ejs:github
   ```

動作確認用のワンライナー:
```bash
yt-dlp -v "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --skip-download \
  --extractor-args "youtube:player_client=mweb,web" \
  --js-runtimes node \
  --remote-components ejs:github
```
ログの最後の方に `[info] dQw4w9WgXcQ: Downloading 1 format(s): 18` のような行が出れば成功です。

## ログイン機能について

`ytdlp_frontend`側でサイト全体にログインを必須にするため、以下のエンドポイントを追加しました:

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/api/auth/signup` | アカウント作成(メールアドレス+パスワード) |
| POST | `/api/auth/login` | ログイン |
| POST | `/api/auth/verify` | セッショントークンの検証 |

- パスワードは`werkzeug.security`でハッシュ化して保存(平文は一切保存しません)
- ユーザー情報は`auth_data/users.json`に保存(`.gitignore`済み)
- セッショントークンは`itsdangerous`による署名付きトークンで、1週間で自動的に無効になります
  (`YTDLP_API_SESSION_SECRET`環境変数、未設定なら自動生成)
- サインアップ/ログイン時のIPアドレスも記録します(`ip`パラメータ、Vercelが検知した
  実際の訪問者IPをフロントエンドが転送してくる想定)

## 管理者パスワードについて

`/api/cache`・`/api/cache/{video_id}`のDELETE(キャッシュの初期化)には管理者パスワードが
必要です。環境変数`YTDLP_API_ADMIN_PASSWORD`を設定するか、未設定なら起動時に自動で
英数字48文字のランダムなパスワードが生成されます(`admin_password.txt`に保存、
起動時のログにも表示されます)。リクエスト側は`?password=`で同じ値を渡してください。

```bash
YTDLP_API_ADMIN_PASSWORD=好きなパスワード python3 server.py
```

```bash
curl -X DELETE "https://あなたの公開URL/api/cache?password=好きなパスワード"
```

検索・動画情報取得などの一般的なエンドポイントには認証を掛けていません
(以前は合言葉(共有シークレット)方式で`/api/*`全体をロックしていましたが、
運用が煩雑だったため撤廃しました)。初期化のような破壊的な操作だけを
管理者パスワードで保護する方針です。公開URLの取り扱いには引き続きご注意ください。

## Termux以外へのデプロイについて

このAPIは元々Termux(常時起動できるスマホ内サーバー)向けに設計していますが、
Railway/Replit/CodeSandboxなどクラウド環境向けの設定ファイルも同梱しています
(`railway.json` / `Procfile` / `.replit` / `replit.nix` / `.codesandbox/tasks.json`)。
`gunicorn server:app`で起動する構成です。

**ただし重要な注意点があります。** このAPIは`admin_password.txt` / `session_secret.txt` /
`auth_data/users.json` / `trending_data/views.json`のような状態をローカルファイルに
保存しています。多くの無料クラウド環境(CodeSandboxの無料枠、Replitの一部プラン等)は
**再起動のたびにファイルシステムがリセットされる**ため、そのたびに管理者パスワードが
変わったり、ログインセッションが全員無効になったり、視聴統計が消えたりします。
永続ストレージが無いプランで使う場合は、この挙動を前提にしてください
(Railway・Replitの有料プランは永続ボリュームに対応しています)。

### Google Apps Script (GAS) について

**GASへのデプロイには対応できません。** GASはJavaScript(V8)しか実行できない環境で、
Pythonで書かれたこのAPIやyt-dlp自体を動かす方法が存在しません。GASには「常時起動する
Webサーバー」という概念も無く(`doGet`/`doPost`でリクエストの都度スクリプトが起動する
方式)、そもそも設計思想が噛み合いません。

## Termuxでのセットアップ

```bash
pkg update && pkg upgrade
pkg install python git clang
# clang は Flask の依存(MarkupSafe等)がソースビルドを要求した場合の保険用。
# 通常は不要ですが、入れておくと余計なビルドエラーを避けられます。
# ffmpegは不要です(HLSリアルタイム変換機能は廃止したため)。

# Pillow(アイコン検証に使用)は pip install だとTermux上でソースビルドになり、
# libjpeg等のヘッダーが無くて失敗する。Termuxが用意しているビルド済みパッケージを使う。
pkg install python-pillow

cd ytdlp_api   # このディレクトリに server.py / requirements.txt を配置

python -m venv venv --system-site-packages
# --system-site-packages を付けることで、venv内からも pkg install した python-pillow を
# そのまま使えるようにしている(付け忘れるとvenv内で改めてPillowが見えず、
# pip install Pillow を試みてまた同じビルドエラーになる)。
source venv/bin/activate
pip install -r requirements.txt

# .envファイルの作成(オーナーのメールアドレスやGroq APIキー等の秘密情報はここに書く)
cp .env.example .env
# .env を開いて YTDLP_API_OWNER_EMAILS 等を書き換えてください
```

**既にvenvを作成済みの場合**(`--system-site-packages`無しで作っていた場合)は、
venvを作り直すのが一番簡単です。

```bash
deactivate
rm -rf venv
python -m venv venv --system-site-packages
source venv/bin/activate
pip install -r requirements.txt
```

## 起動

```bash
source venv/bin/activate
python3 server.py
# デフォルトで 0.0.0.0:5000 で待受
# ポートを変えたい場合: YTDLP_API_PORT=8080 python3 server.py
```

バックグラウンドで維持したい場合:

```bash
termux-wake-lock
tmux new -s ytdlp_api
# tmux内で python3 server.py を実行し、Ctrl+B, D でデタッチ
```

## 公開方法

DuckDNS + ルーターのポート開放だけだと、モバイル回線のCGNATなど環境によっては
他の端末から繋がらないことがあります。その場合はトンネル系サービスを使うのが確実です。

### Cloudflare Tunnel(おすすめ、無料・固定URL可)

```bash
pkg install cloudflared
```

**お試しで使う場合**(起動のたびにURLが変わる):

```bash
cloudflared tunnel --url http://localhost:5000
```
表示された `https://xxxx.trycloudflare.com` が外部公開URLです。

**固定ドメインで使う場合**(Cloudflareに登録済みの独自ドメインが必要):

```bash
cloudflared tunnel login
cloudflared tunnel create ytdlp-api
cloudflared tunnel route dns ytdlp-api api.あなたのドメイン.com
cloudflared tunnel run --url http://localhost:5000 ytdlp-api
```

これで `https://api.あなたのドメイン.com` が固定URLとして使えます。
`tmux`で`python3 server.py`と`cloudflared tunnel run ...`を別ウィンドウで起動しておいてください。

### ngrok

```bash
cd ~
curl -o ngrok.tgz https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz
tar xvzf ngrok.tgz
./ngrok config add-authtoken <あなたのauthtoken>
./ngrok http 5000
```

表示された `https://xxxx.ngrok-free.app` が外部からのアクセス先です。

いずれの方法でも、ブラウザで `https://あなたの公開URL/api/stats` を開けば、
リアルタイムのダッシュボードが見られます。

## 動作確認例

```bash
# ブラウザで開けばAPI一覧+実行テストUIが使える
https://あなたの公開URL/api

curl "https://あなたの公開URL/api/info/dQw4w9WgXcQ"
curl "https://あなたの公開URL/api/stream/dQw4w9WgXcQ"
curl "https://あなたの公開URL/api/search?q=猫&limit=10"
curl "https://あなたの公開URL/api/trending?limit=24"

# ブラウザで開く
https://あなたの公開URL/api/stats
```

## HTTP/SOCKSプロキシについて(VPNの代替)

VPNだと接続断や自動再接続の制御がしづらいため、環境変数 **`YTDLP_API_PROXY_URL`** で
HTTP/SOCKSプロキシを直接指定できるようにしています。

```bash
YTDLP_API_PROXY_URL=http://user:pass@proxy.example.com:8080 python3 server.py
# SOCKS5の場合(要 pip install "yt-dlp[default]" 相当、PySocksが必要になることがあります)
YTDLP_API_PROXY_URL=socks5://127.0.0.1:1080 python3 server.py
```

設定すると、以下の全ての通信経路がこのプロキシ経由になります。

- yt-dlp本体(`_ydl_opts`の`proxy`オプション)
- 生スクレイピング系(`_fetch_page`, `_fetch_youtube_continuation`。urllibのProxyHandler経由)
- アバター/バナー等の画像取得(`_http` requestsセッション)

未設定の場合は今まで通り直接通信します。VPNと違い、プロキシ自体の接続断への対応は
`_extract`の自動リトライ(下記)と組み合わせて使う想定です。

## アカウント連携(登録チャンネル・高評価動画)

登録チャンネル・高評価した動画を、ログインアカウントに紐づけてサーバー側
(`user_subscriptions` / `user_likes`テーブル)にも保存するようにしました。
ブラウザのlocalStorageへの保存はそのまま維持しつつ、ログイン中は追加でサーバーにも
同期する形です(別端末で同じアカウントにログインすれば同じデータが見られます)。

- `X-Session-Token`ヘッダーで渡されたセッショントークンを検証し、emailを特定します。
- `/api/user/subscriptions`(GET/POST)、`/api/user/subscriptions/{channel_id}`(DELETE)
- `/api/user/likes`(GET/POST)、`/api/user/likes/{video_id}`(DELETE)

## 起動時の自動クリーンアップ

サーバー起動時(再起動時含む)に、期限切れのレスポンスキャッシュを自動削除します
(`_cleanup_expired_response_cache`)。以前から`_response_cache_set`のたびに古いものを
削除する仕組みはありましたが、リクエストが来ないまま長時間経つと反映されないため、
起動時にも独立して掃除するようにしています。

## ログの見た目について

`log(tag, message, color)`という共通のログ関数を用意し、時刻・色付きタグ付きの
統一されたフォーマットで出力するようにしました(例: `12:31:12 [ACCESS] ...`)。
色はターミナルがTTYの場合だけ有効になります(ファイルにリダイレクトした時に
制御文字が混ざらないように)。

## アカウント設定・お問い合わせについて

- `GET/PUT /api/user/me` でプロフィール(表示名・ユーザーID・アイコン)を管理できます。
  - アイコンは50KB以下・正方形のみ(Pillowで実際の画像として検証し、こちら側でPNGに
    正規化してから保存するので、クライアントが自称するcontent-typeは信用しません)。
  - 表示名・ユーザーIDはXSS対策として危険な文字(`< > " ' &`)を弾いています。
  - アイコンは1ユーザーにつき1枚だけ持つ形なので、変更すれば自動的に前のデータは
    上書きされて残りません。
- `OWNER_EMAILS`(環境変数`YTDLP_API_OWNER_EMAILS`、カンマ区切りで複数指定可)で
  指定したメールアドレスだけが「オーナー」になり、お問い合わせに対応できます。
  - `POST /api/inquiries` で誰でも送信できます。
  - `GET /api/inquiries` は、一般ユーザーは自分が送った分だけ、オーナーは全員分を返します。
  - `GET /api/inquiries/{id}` `POST /api/inquiries/{id}/replies` も同様に、本人か
    オーナーでなければ404になります(他人のお問い合わせの存在自体が分からないように)。
  - `GET /api/inquiries/{id}` は、発言者ごとのアイコン・表示名(`avatars`)も一緒に返します。
    フロントエンド側でLINEのような吹き出しUIに使っています。
  - 削除(`DELETE /api/inquiries/{id}`)はオーナーのみ可能です(一般ユーザーは403)。

## 自分のプレイリスト機能について

YouTube本家のように、ログイン中のアカウントで自分のプレイリストを作れます
(`user_playlists` / `user_playlist_videos`テーブル)。

- **1人あたり最大10個まで**プレイリストを作成できます(超えると409)。
- プレイリスト名は**20文字まで**です。
- 1つのプレイリストに**最大100本まで**動画を追加できます(超えると409)。
  同じ動画を重複して追加しようとした場合はエラーにはせず、何もしません。
- 他人のプレイリストは本人以外アクセスできません(404で保護、存在自体も分かりません)。
- YouTube本家の再生リストをそのまま見る機能(`/playlist?list=...`)とは完全に別物です。
  こちらはYouTube側のデータをそのまま表示するだけで、書き込み(動画の追加・削除)は
  できません。フロントエンド側でも、画面に出るバッジの色を変えて区別しています。

## 視聴履歴について

個人アカウントに紐づけるのではなく、**このサイトで実際に見られた動画を
1つの共有フィードとして持てるようにしました**(SQLite、`watch_history`テーブル)。
誰がいつ見たかは記録せず、動画ごとの最終視聴時刻と**視聴回数(`view_count`)**だけを
保持しています。フロントエンド側では「このサイトでN回視聴」という形で表示されます。

- 以前の(既に削除した)「視聴回数を集計してトレンドに使う」機能とは無関係です。
  トレンド表示には一切使っていません。
- サイト全体で直近200件まで保存し、それを超えた古いものは自動的に消えます。
- 閲覧(`GET`)・記録(`POST`)はログインさえしていれば誰でもできます。個人を
  特定する情報(email等)は一切保存しません。
- 全削除(`DELETE`)は管理者パスワードが必要な破壊的操作として保護しています。
- 同じ動画が再度視聴されると`view_count`が+1され、`watched_at`(最終視聴日時)も
  更新されます(一覧の並び順は`watched_at`降順)。
- カラムを追加した際、以前のバージョンで既に作られていた`watch_history`テーブルには
  自動的にカラムが追加されます(`_init_db`内でマイグレーション、`sqlite3.OperationalError:
  no column named ...`エラーへの対応)。既存データも壊れません。
- **さらに古い(個人アカウント別だった頃の`PRIMARY KEY (email, video_id)`)テーブルが
  残っている場合は、カラム追加だけでは主キー構造まで直せない(SQLiteのALTER TABLEは
  主キーを変更できない)ため、テーブルごと作り直します**(`ON CONFLICT clause does not
  match any PRIMARY KEY or UNIQUE constraint`エラーへの対応)。この場合のみ、個人別の
  古い履歴データは失われますが、今の「サイト全体で共有」の仕様には元々合わないデータ
  なので実害はありません。

## 検索・関連動画での再生リスト表示について

検索結果や関連動画に再生リストが混じっている場合、YouTubeの新しいカード形式
(`lockupViewModel`)経由で拾われる際に**サムネイルが出ない・`entry_type`が
設定されない(動画として誤認識される)不具合がありました**。原因は2つ:

1. `_parse_lockup_view_model`が動画・再生リストを区別しておらず、再生リストの
   `contentId`(実際は再生リストID)を動画IDとして扱っていた
2. 再生リストのサムネイルは`contentImage.collectionThumbnailViewModel.primaryThumbnail...`
   という入れ子になっていることがあり(YouTube側の実装がここ数ヶ月で何度か変わっている)、
   動画用の経路(`contentImage.thumbnailViewModel...`)しか見ていなかった

`contentType`(`LOCKUP_CONTENT_TYPE_PLAYLIST`等)を見て判定し、サムネイルは
新旧両方の経路を試すように修正しました。

## エラーコードについて

エラー時のレスポンスは、以下のような形式で返ります。

```json
{ "detail": "人が読むためのメッセージ(日本語)", "code": "VALIDATION_INVALID_EMAIL" }
```

`detail`は表示用の文言(変更されることがあります)、`code`は**文言に依存せず機械的に
エラーの種類を判別するための識別子**です。フロントエンド側でエラーの種類ごとに
処理を分けたい場合は、`detail`の文字列比較ではなく`code`を見てください。

### エラーコード一覧

| コード | 意味 |
|---|---|
| **認証(AUTH_*)** | |
| `AUTH_INVALID_CREDENTIALS` | メールアドレスまたはパスワードが違う |
| `AUTH_EMAIL_ALREADY_REGISTERED` | そのメールアドレスは既に登録済み |
| `AUTH_SESSION_INVALID` | セッションが無効・期限切れ |
| `AUTH_LOGIN_REQUIRED` | ログインが必要な操作にログインせずアクセスした |
| `AUTH_SIGNUP_BLOCKED` | BAN済みIP/メールからの新規登録試行 |
| `AUTH_INVALID_ADMIN_PASSWORD` | 管理者パスワードが違う |
| **入力値検証(VALIDATION_*)** | |
| `VALIDATION_INVALID_EMAIL` | メールアドレスの形式が不正 |
| `VALIDATION_PASSWORD_TOO_SHORT` | パスワードが8文字未満 |
| `VALIDATION_TERMS_NOT_AGREED` | 利用規約に同意していない |
| `VALIDATION_TOKEN_REQUIRED` | トークンパラメータが無い |
| `VALIDATION_DISPLAY_NAME_REQUIRED` / `_TOO_LONG` / `_INVALID_CHARS` | 表示名の検証エラー |
| `VALIDATION_USER_ID_REQUIRED` / `_INVALID_FORMAT` | ユーザーIDの検証エラー |
| `VALIDATION_AVATAR_INVALID_FORMAT` / `_CORRUPTED` / `_TOO_LARGE` / `_UNREADABLE` / `_NOT_SQUARE` | アイコン画像の検証エラー |
| `VALIDATION_FIELD_NOT_STRING` / `_REQUIRED` / `_TOO_LONG` | お問い合わせ等、汎用フィールドの検証エラー |
| `VALIDATION_PLAYLIST_NAME_NOT_STRING` / `_REQUIRED` / `_TOO_LONG` | プレイリスト名の検証エラー |
| `VALIDATION_WORD_REQUIRED` / `_TOO_SHORT` / `_TOO_LONG` | NGワードの検証エラー |
| `VALIDATION_POLICY_TOO_LONG` | AI判定基準が長すぎる |
| `VALIDATION_VIDEO_ID_REQUIRED` | video_idパラメータが無い |
| `VALIDATION_QUERY_REQUIRED` | 検索クエリが無い |
| `VALIDATION_CHANNEL_ID_REQUIRED` | channel_idパラメータが無い |
| `VALIDATION_IP_REQUIRED` / `VALIDATION_EMAIL_REQUIRED` | BAN操作時のIP/メール未指定 |
| `VALIDATION_INVALID_URL` | 一括インポート時のURLが不正 |
| **モデレーション/制限** | |
| `MODERATION_BANNED` | NGワード・AI判定によりBANされている |
| `PERMISSION_OWNER_ONLY` | オーナー限定機能への非オーナーアクセス |
| `RATE_LIMIT_COOLDOWN` | お問い合わせの連続送信 |
| `RATE_LIMIT_DAILY_EXCEEDED` | お問い合わせの1日の上限超過 |
| `LIMIT_PLAYLIST_COUNT_EXCEEDED` | プレイリスト作成数の上限超過 |
| `LIMIT_PLAYLIST_VIDEOS_EXCEEDED` | プレイリスト内動画数の上限超過 |
| `CONFLICT_USER_ID_TAKEN` | ユーザーIDが既に使われている |
| **見つからない(NOT_FOUND_*)** | |
| `NOT_FOUND_USER` / `NOT_FOUND_INQUIRY` / `NOT_FOUND_PLAYLIST` / `NOT_FOUND_BANNED_WORD` / `NOT_FOUND_CACHE` / `NOT_FOUND_SUBTITLE` / `NOT_FOUND_SUBTITLE_URL` / `NOT_FOUND_LIVE_CHAT` / `NOT_FOUND_LIVE_CHAT_URL` / `NOT_FOUND_DIRECT_URL` | それぞれのリソースが存在しない |
| **外部サービス関連(UPSTREAM_*)** | |
| `UPSTREAM_FETCH_FAILED` / `UPSTREAM_PARSE_FAILED` / `UPSTREAM_PAGE_FETCH_FAILED` / `UPSTREAM_WATCH_PAGE_FETCH_FAILED` / `UPSTREAM_SUBTITLE_FETCH_FAILED` / `UPSTREAM_LIVE_CHAT_FETCH_FAILED` / `UPSTREAM_CONTINUATION_FETCH_FAILED` / `UPSTREAM_CONTINUATION_PARSE_FAILED` / `UPSTREAM_MEDIA_FETCH_FAILED` | YouTube側のページ・データ取得に失敗 |
| **動画抽出(EXTRACTION_*)** | |
| `EXTRACTION_FAILED` | yt-dlpでの抽出全般が失敗 |
| `EXTRACTION_NO_PLAYABLE_DATA` | 再生可能なフォーマット/HLSが1つも無い |
| **その他** | |
| `NETWORK_UNSTABLE` | 一時的なネットワーク不調(自動リトライ後もダメだった) |
| `SERVER_BUSY` | Node.js同時実行数の上限に達し、待機後もタイムアウト |
| `UNKNOWN_ERROR` | コード未分類(基本的に発生しないはずの、移行漏れ検出用) |

## モデレーション(NGワード・IPバン)について

検索キーワード・視聴した動画の**タイトル**にNGワードが含まれていた場合、
そのリクエスト元IP(と、ログイン中ならメールアドレスも)を自動的にBANする機能です。

- **動画の判定はタイトルのみで行い、説明文は見ません。** 説明文には
  「この動画には〇〇的な内容は含まれません」のような注意書き・免責事項が
  書かれていることがあり、そうした文がNGワードに誤ってヒットして無関係な動画まで
  誤BANしてしまうことがあるためです。タイトルの方が実際のコンテンツを直接的に
  表しているので、判定材料として使っています。
- **誤検知対策として、英数字だけのNGワードは単語境界でのマッチだけを見ます。**
  例えば「ass」を登録しても、「assassin」「class」のような無関係な単語には
  反応しません(いわゆる「Scunthorpe問題」への対策)。日本語混じりのワードは
  単語境界という概念が無いため部分一致のままですが、1文字だけのような極端に
  短いワードは誤検知が多すぎるため登録できません(2文字以上必須)。
- **NGワードのリストは空の状態で出荷しています。** 何が「不適切」かの判断は
  こちらでは行わず、サイト運営者(オーナー)が管理画面(`/admin/moderation`、
  フロントエンド側)から自分で登録する方針です。
- 1つずつの登録に加えて、**まとめて登録**(改行/カンマ区切りのテキスト)、
  **URLから一括インポート**(外部で公開されているNGワードリストのURLを指定して
  丸ごと取り込む)にも対応しています。
  - 参考: [LDNOOBW](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words)
    (多言語対応、多くのサービスで実際に使われている定番リスト)
  - 参考: [MosasoM/inappropriate-words-ja](https://github.com/MosasoM/inappropriate-words-ja)
    (日本語の不適切表現リスト、`textlint`等でも参照元として使われている)
  - どちらも中身の単語はこちらでは一切選定・生成していません。GitHubの該当ファイルを
    開いて「Raw」ボタンからURLを取得し、それを管理画面に貼り付けてください。
- **IPアドレスに加えて、メールアドレス単位でもBANできます。** ログイン中に
  NGワードに引っかかった場合、IPだけでなくそのアカウントのメールアドレスも
  一緒にBANされます(`banned_emails`テーブル)。IP・メールのどちらかがBAN
  されていればアクセスを制限します。
- **AI(Groq)による動画タイトルの追加判定にも対応しています(任意機能)。**
  NGワードに引っかからなかった場合でも、Groqに動画タイトルを判定させることが
  できます。
  - `.env`(`.env.example`をコピーして作成、server.pyと同じディレクトリに置く)に
    `YTDLP_API_GROQ_API_KEY`を設定してください。APIキーは
    [console.groq.com](https://console.groq.com)で無料で発行できます
    (クレジットカード登録不要。無料枠・モデルラインナップは変更されることが
    あるので、最新状況は公式サイトでご確認ください)。
  - **「何を不適切とみなすか」の判定基準は、こちらでは決めていません。** 管理画面
    (`/admin/moderation`)から、オーナーが基準文を自由に入力する形です。
    基準文が空欄のままなら、AI判定自体行われずNGワードだけで動作します。
  - モデル名は`YTDLP_API_GROQ_MODEL`で変更できます(既定は
    `llama-3.3-70b-versatile`)。Groq側のモデルラインナップはよく変更されるため、
    エラーが出るようになったら最新のモデル名に書き換えてください。
  - API呼び出しに失敗した場合は、誤って利用者をBANしないよう判定をスキップします
    (fail open)。
  - AIが「不適切」と判定した理由は、BANログにそのまま記録されます。
- **BAN済みの場合、お問い合わせ・ログイン関連の最低限のAPI以外全て403になります。**
  フロントエンド側でも「お問い合わせ以外は使えない」案内ページに誘導されます。
- BANのログ(`ban_events`テーブル)には、**IPアドレスだけでなく、分かっている
  場合はメールアドレスも記録**されます。誤BANの可能性を確認できるよう、
  管理画面からIPまたはメールアドレスで絞り込んで確認できます。
- 手動でのBAN・解除も管理画面からできます。
- お問い合わせには荒らし対策として、**1日5件まで・連続送信は60秒間隔まで**の
  制限を設けています。
- **管理画面ではNGワードの中身(実際の単語)を一覧表示しません。** 登録件数だけを
  表示し、削除は「削除したい単語をそのまま入力する」方式にしています。
  (画面に不適切な単語がずらっと並ぶこと自体を避けるため。)
- **IPがBANされている場合、新規アカウント作成もできません。**
  (BAN逃れのための新規登録を防ぐため。)

## Node.js同時実行数の制限について

動画取得のたびに行うNode.jsでの署名解読は、スマホ(Termux)のCPU・メモリに対して
それなりに重い処理です。同時に何本も走ると全体が詰まって遅くなるため、

- **同時に実行できるのは3本まで**です(`_NODE_EXTRACT_MAX_CONCURRENT`)
- 4本目以降のリクエストは、枠が空くまで最大90秒待機します
  (`_NODE_EXTRACT_WAIT_TIMEOUT_SEC`)
- 90秒待っても空かない場合は、HTTP 503(サーバーが混み合っています)を返します

なお、以前試していた「android_vrクライアントによる高速プレビュー」機能は、
実運用で**返ってくるストリームURLが既に期限切れで再生できない**ことが確認された
ため撤去しました。速度よりも確実に再生できることを優先し、Cookie付き・
Node.js署名解読ありの、従来通りの安定した方式のみを使う構成に戻しています。

## ライブ配信について

ライブ配信で`No video formats found!`エラーになる、yt-dlp側の既知の問題(2026年に入って
複数のIssueが立っている、PO Token絡みでフォーマットが弾かれてしまう不具合)への対策を
入れています。

- `extractor_args`に`youtube: {formats: [missing_pot]}`を追加し、PO Tokenが無いことを
  理由に除外されていたフォーマットも許可するようにしました。
- それでも1つもフォーマットが見つからなかった場合に備えて`ignore_no_formats_error`も
  有効にし、エラーで落ちる代わりに`manifest_url`(HLSのマスタープレイリストURL)だけでも
  拾えるようにしています。
- `--live-from-start`は使っていません。むしろこのオプションを使うと同じエラーが
  発生するという報告が複数あるため、あえて外しています。
- 最終的に再生可能なデータ(`streams`・`hls_url`のどちらか)が1つも無かった場合は、
  空っぽのレスポンスをそのまま返さず、明示的なエラー(400)にしています。

## 注意点

- **`/api/stream`が動画詳細(info相当)も一緒に返すようになりました。**
  以前はフロントエンドが`/api/info`と`/api/stream`を毎回両方叩いていて、重いyt-dlp抽出
  (Node.jsでの署名解読含む)が動画1本につき2回走っていました。`/api/stream`のレスポンスに
  `/api/info`の内容をマージするようにしたことで、フロントエンドは`/api/stream`を1回
  呼ぶだけで済むようになっています(`ytdlp_frontend`側もこれに合わせて更新済み)。
  `/api/info`自体は他の用途のために軽量なまま残してあります(互換性のため)。
- 念のため、直近120秒以内の抽出結果をメモリ上に短時間キャッシュする仕組み
  (`_extract_full`)も残しています。何らかの理由で同じ動画に対して短時間に
  複数回抽出が走った場合の保険です。

- **VPN切断・回線の瞬断などでyt-dlpの取得が失敗した場合、2回まで自動リトライします
  (`_extract`関数)。** それでも失敗する場合は、以前は一律HTTP 400を返していましたが、
  現在はネットワーク起因のエラーだと判定できた場合に限り**503**を返すように修正しました
  (400は「リクエスト自体がおかしい」という意味なので、一時的な接続断には不適切でした)。
  動画が存在しない・非公開などの本当のエラーはリトライせず、これまで通り400のままです。
  なお、VPN接続自体の自動再接続はこのアプリの管轄外です(TermuxからVPNアプリを直接
  制御する手段が無いため)。WireGuard/OpenVPNをCLIで使っている場合は、接続監視・
  再接続をする別のシェルスクリプトをTermux側で用意することをおすすめします。
  VPNの代わりに`YTDLP_API_PROXY_URL`でプロキシを使う方法もあります(上記参照)。

- `search`/`related`/`trending`/`playlist`が共有している動画カード抽出(`_parse_video_cards`)は、
  従来形式(`videoRenderer`等)に加えて、YouTubeの新しいUI形式(`lockupViewModel`)にも
  対応しています。この新形式は`videoId`/`title`が直下のキーではなく大きく構造が違うため、
  元々の「videoId+titleを直下に持つノードを総当たり」という判定方式では拾えていませんでした
  (気づかないうちに一部の結果が抜け落ちていた可能性があります)。

- `server.py`は`#`コメントを全部除去した状態で管理しています(docstringは巨大な
  埋め込みHTML文字列を壊すリスクがあるため保持)。コードを読みながら手を加える場合は
  ご注意ください。
- cookieが必要なサイト用には `_ydl_opts` に `"cookiefile": "cookies.txt"` を追加してください。
- このAPI自体には認証機構は入っていません(`ytdlp_frontend`側でサイト全体にログインを
  必須にしていますが、`ytdlp_api`のURLを直接知っていれば誰でも叩けます)。公開URLを
  不特定多数に共有しないよう注意してください。
- 全てのYouTubeアクセスに `hl=ja&gl=JP`(生スクレイピング側)/ `extractor_args: {"youtube": {"lang": ["ja"]}}`
  (yt-dlp側)を明示的に指定しています。これにより、オリジナルが日本語のタイトルなのに
  英語へ自動翻訳されて返ってくる現象をできるだけ抑えています。
- 生スクレイピング(`_fetch_page`)・yt-dlp呼び出しの両方に、本物のChromeブラウザに近い
  ヘッダー一式(`Accept`/`Sec-Ch-Ua`/`Sec-Fetch-*`等)を付与してBot判定を受けにくくして
  います。`Accept-Encoding`でgzip圧縮を許可している都合上、`urllib`は自動展開しないため
  `_decompress_body`で手動展開しています(文字化け防止)。
- `/api/proxy-stream` は googlevideo 等のCDNへのリクエストにブラウザっぽい`User-Agent`を
  付けています(素のPython requestsのUAだとCDN側に弾かれることがあるため)。
- `/api/proxy-stream` はクライアント側が接続を切った(シーク/画質切替/ページ離脱等)場合の
  `ChunkedEncodingError`/`ConnectionError`を握りつぶすようにしています。よくある正常な
  切断でもエラーログが大量に出てしまっていたための対応です。
- `/api/channel` のアバター画像取得は次の順で試します: ①`<meta property="og:image">`
  ②`channelMetadataRenderer`をピンポイントで狙ったytInitialData探索 ③汎用の"avatar"
  総当たり ④yt-dlp側のthumbnails。1つの方式に頼らず段階的にフォールバックすることで
  取得できないケースを減らしています(バナーは`og:image`に相当するメタタグが無いため、
  ytInitialData方式のみです)。
- `/api/info` のチャンネルアバターは`videoOwnerRenderer`をピンポイントで狙って探します。
  以前は"avatar"という名前のノードを単純に最初に見つけたもので済ませていたため、
  コメント投稿者など別人のアイコンを誤って拾ってしまうことがありました。
- 生ページのスクレイピング(`related`/`trending`/`channel`のアバター・バナー取得)は、
  `requests`ではなくPython標準ライブラリの`urllib`で行っています。`requests`(urllib3)だと
  ブロックされるケースでも、`urllib`だとTLS/HTTPスタックのフィンガープリントが変わって
  通ることがあるための対策です。
- `/api/info` にもチャンネルアバター(`channel_avatar` / `channel_avatar_base64`)、
  `/api/comments` にもコメント投稿者のアイコン(`author_thumbnail`)を追加しています。
