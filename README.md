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
| GET | `/api/playlist/{playlist_id}` | プレイリストのメタ情報+収録動画一覧。`?limit=&offset=`で範囲指定。7時間キャッシュ |
| GET | `/api/channel/{channel_id}` | チャンネルのメタ情報+投稿動画一覧。`@handle`/`UCxxxx`/フルURLいずれも可。7時間キャッシュ |
| GET | `/api/comments/{video_id}` | 動画のコメント一覧。`?limit=`で件数指定。7時間キャッシュ |
| GET | `/api/related/{video_id}` | 関連動画。動画ページの`ytInitialData`を解析して取得(非公式、後述) |
| GET | `/api/trending` | おすすめ/トレンドフィード(非ログイン・非パーソナライズ)。フロントのトップページ用 |
| GET | `/api/info/{video_id}` | 動画の全メタデータ(ストリームURLは含まない) |
| GET | `/api/stream/{video_id}` | その動画の**全ストリームURL一覧**。HLS(m3u8)直リンクがあれば`hls_url`に入る |
| GET | `/api/stats` | **HTMLダッシュボード**。worker/処理中/キャッシュ件数/稼働時間を2秒おきに自動更新表示 |
| GET | `/api/stats/data` | ↑と同じ内容をJSONで返す(自作クライアント用) |
| GET | `/api/workers` | このサーバー(worker)の情報 |
| GET | `/api/processing` | 現在処理中のvideo_id一覧 |
| GET | `/api/cache` | これまでに解決した動画の一覧(`?q=`検索、`?limit=`、`?offset=`) |
| GET | `/api/cache/{video_id}` | キャッシュ済み単一動画の情報 |
| DELETE | `/api/cache/{video_id}` | 一覧インデックスから削除 |

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

いずれも「一覧」はyt-dlpの`extract_flat`(各動画を1本ずつ深掘りしない高速モード)で取得しています。
返ってくる`entries`の各要素は `video_id / title / url / duration / view_count / channel / channel_id / thumbnail` 程度の軽量情報です。
動画1本ごとの全メタデータが必要な場合は、返ってきた`video_id`で改めて `/api/info/{video_id}` を叩いてください。

```bash
curl "https://xxxx.ngrok-free.app/api/search?q=猫&limit=10"
curl "https://xxxx.ngrok-free.app/api/playlist/PLxxxxxxxxxxxx?limit=50"
curl "https://xxxx.ngrok-free.app/api/channel/@handle?limit=30"
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
  "cache_ttl_seconds": 25200
}
```

- `streams` にその動画で利用可能な**全フォーマット**の直リンクが並びます(映像/音声別、解像度別など)。
- `hls_url` はyt-dlpが把握しているネイティブHLS(m3u8)直リンク(YouTubeのライブ配信等で存在)。VODなど無い場合は`null`。

> 以前あったサーバー側でのHLSリアルタイム変換機能(ffmpeg使用、`/api/hls/*`)は廃止しました。
> ffmpegのインストールも不要です。再生には `streams[].url` か、あれば `hls_url` をそのまま使ってください。

## Termuxでのセットアップ

```bash
pkg update && pkg upgrade
pkg install python git clang
# clang は Flask の依存(MarkupSafe等)がソースビルドを要求した場合の保険用。
# 通常は不要ですが、入れておくと余計なビルドエラーを避けられます。
# ffmpegは不要です(HLSリアルタイム変換機能は廃止したため)。

cd ytdlp_api   # このディレクトリに server.py / requirements.txt を配置

python -m venv venv
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

## 注意点

- cookieが必要なサイト用には `_ydl_opts` に `"cookiefile": "cookies.txt"` を追加してください。
- 認証機構は入っていないので、公開URLを不特定多数に共有しないよう注意してください。
