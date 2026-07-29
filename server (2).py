"""
ytdlp_api - yt-dlp を使ったシンプルな動画情報/ストリーム一覧API
UIなし(/api/statsのみUIあり)、API専用。Termux + ngrok での運用を想定。

Flask + requests のみで構成(fastapi/pydanticは不使用。Rustビルド不要)。
ffmpegも不要(HLSリアルタイム変換機能は廃止し、yt-dlpが把握しているm3u8直リンクのみ返す構成)。

エンドポイント:
  GET    /api                                  API一覧・説明・実行テスト用ページ(HTML)
  GET    /api/search                           YouTube検索(?q=検索語&limit=件数)
  GET    /api/playlist/{playlist_id}            プレイリストのメタ情報+収録動画一覧
  GET    /api/channel/{channel_id}              チャンネルのメタ情報+投稿動画一覧+アバター/バナー(base64)
  GET    /api/comments/{video_id}               動画のコメント一覧
  GET    /api/related/{video_id}                関連動画(watch pageのytInitialDataを解析。非公式)
  GET    /api/trending                          このサイトで実際に視聴された動画のランキング
  GET    /api/info/{video_id}                 動画の全メタデータ(ストリームURLは含まない)
  GET    /api/stream/{video_id}                その動画の全ストリームURL一覧 + HLS(m3u8)直リンク(あれば)
  GET    /api/health                           死活監視
  GET    /api/stats                            worker/処理中/キャッシュ/稼働時間を見るダッシュボード(HTML)
  GET    /api/stats/data                       ↑と同じ内容をJSONで返す(ポーリング用)
  GET    /api/workers                          このサーバー(worker)の情報一覧
  GET    /api/processing                       現在処理中のvideo_id一覧(経過時間つき)
  GET    /api/cache                            これまでに解決した動画の一覧(Video ID / Title)
  GET    /api/cache/{video_id}                 キャッシュ済みの単一動画の情報
  DELETE /api/cache/{video_id}                 キャッシュから削除(一覧インデックス+レスポンスキャッシュ)
  DELETE /api/cache                            キャッシュを全部削除(強制リフレッシュ用)

video_id には
  - YouTubeの動画ID (例: dQw4w9WgXcQ)
  - もしくはURLエンコードした完全なURL (例: https%3A%2F%2Fvimeo.com%2F12345)
のどちらも指定できます。単純な文字列(URLでない)場合は自動的に
https://www.youtube.com/watch?v={video_id} として扱われます。

cookies.txtについて:
  server.pyと同じディレクトリに "cookies.txt" (Netscape形式)を置くと、
  yt-dlp・生スクレイピングの両方で自動的に使われます(年齢制限動画やBot判定回避に有効)。
  環境変数 YTDLP_API_COOKIES_FILE で置き場所を変更できます。詳しくはREADME参照。

レスポンスキャッシュについて:
  /api/info と /api/stream は、同じvideo_idに対する結果を7時間(YTDLP_API_CACHE_TTL_SECONDS)
  保存し、期間内の再リクエストはyt-dlpを呼ばずに即座にキャッシュを返します。
  レスポンスの "_cache" フィールドで hit/miss と残り有効時間が確認できます。
  ※ CDN側の直リンク(googlevideo等)は数時間で失効することがあるため、
     再生に失敗する場合はキャッシュ有効期間内でも一度削除して取り直してください
     (DELETE /api/cache/{video_id} で個別に、DELETE /api/cache で全部まとめて削除できます)。
"""

import os
import re
import json
import time
import base64
import sqlite3
import threading
import urllib.parse
import urllib.request
import http.cookiejar
import urllib.error
import contextlib
from datetime import datetime

import requests
import yt_dlp
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

TMP_DIR_PATH = os.environ.get("YTDLP_API_TMP", os.path.join(os.path.expanduser("~"), "ytdlp_api_tmp"))
os.makedirs(TMP_DIR_PATH, exist_ok=True)

# cookies.txt (Netscape形式)。年齢制限/メンバー限定動画へのアクセスや、
# Bot判定の回避に使う。既定ではserver.pyと同じディレクトリに置くだけで拾われる。
# 無ければ何もしない(これまで通りcookie無しで動く)。
COOKIES_FILE_PATH = os.environ.get(
    "YTDLP_API_COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
)


def _load_cookiejar():
    """cookies.txt (Netscape形式)を読み込む。無い/壊れてる場合はNoneを返すだけで、
    呼び出し側はcookie無しの状態にフォールバックできる。"""
    if not os.path.isfile(COOKIES_FILE_PATH):
        return None
    jar = http.cookiejar.MozillaCookieJar(COOKIES_FILE_PATH)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return None
    return jar


def _cookie_header_string():
    """urllib(_fetch_page)用に、cookies.txtの中身をCookieヘッダの文字列にする。
    consent回避用のCONSENT/SOCSは常に含め、cookies.txtがあればそれも足す。"""
    parts = ["CONSENT=YES+1", "SOCS=CAI"]
    jar = _load_cookiejar()
    if jar:
        for cookie in jar:
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


# /api/info, /api/stream の結果を保存する期間
RESPONSE_CACHE_TTL_SECONDS = int(os.environ.get("YTDLP_API_CACHE_TTL_SECONDS", str(7 * 3600)))  # 7時間

# キャッシュ削除など破壊的な操作を保護するパスワード。未設定だと誰でも削除できてしまうので、
# 未設定の場合は削除系エンドポイントを常に拒否する(空文字列だと事故で誰でも通ってしまうため)。
ADMIN_PASSWORD = os.environ.get("YTDLP_API_ADMIN_PASSWORD", "")


def _require_admin_password():
    if not ADMIN_PASSWORD:
        raise ApiError(503, "admin password is not configured on the server (set YTDLP_API_ADMIN_PASSWORD)")
    supplied = request.args.get("password", "")
    if supplied != ADMIN_PASSWORD:
        raise ApiError(403, "invalid password")


# ---------- 自前トレンド (このサーバー経由で実際に視聴された動画の集計) ----------
#
# YouTube公式のトレンドページのスクレイピングはBot対策等で不安定だったため、
# 代わりに「このAPI経由で/api/infoが叩かれた回数」を自前で集計してトレンドとして使う。
# .jsonファイルに素朴に貯めていくだけの、種類別に分けたシンプルな構成。

TRENDING_DATA_DIR = os.path.join(TMP_DIR_PATH, "trending_data")
os.makedirs(TRENDING_DATA_DIR, exist_ok=True)
VIEWS_JSON_PATH = os.path.join(TRENDING_DATA_DIR, "views.json")  # video_id -> 視聴統計
_TRENDING_LOCK = threading.Lock()


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json_file(path, data):
    # 書き込み中にプロセスが落ちてファイルが壊れないよう、一時ファイルに書いてから置き換える
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def _record_view(video_id, data):
    """/api/info が呼ばれるたびに1回分の視聴として記録する。"""
    with _TRENDING_LOCK:
        views = _load_json_file(VIEWS_JSON_PATH)
        entry = views.get(video_id, {"view_count": 0})
        entry["view_count"] = entry.get("view_count", 0) + 1
        entry["title"] = data.get("title") or entry.get("title")
        entry["channel"] = data.get("channel") or data.get("uploader") or entry.get("channel")
        entry["channel_id"] = data.get("channel_id") or entry.get("channel_id")
        entry["thumbnail"] = data.get("thumbnail") or entry.get("thumbnail")
        entry["duration"] = data.get("duration") or entry.get("duration")
        entry["last_viewed"] = time.time()
        views[video_id] = entry
        _save_json_file(VIEWS_JSON_PATH, views)


def _get_local_trending(limit):
    with _TRENDING_LOCK:
        views = _load_json_file(VIEWS_JSON_PATH)
    ranked = sorted(views.items(), key=lambda kv: kv[1].get("view_count", 0), reverse=True)
    entries = []
    for video_id, entry in ranked[:limit]:
        entries.append({
            "video_id": video_id,
            "title": entry.get("title"),
            "channel": entry.get("channel"),
            "channel_id": entry.get("channel_id"),
            "channel_thumbnail": None,
            "thumbnail": entry.get("thumbnail"),
            "duration": entry.get("duration"),
            "view_count_text": f"このサイトで{entry.get('view_count', 0)}回視聴",
            "length_text": None,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return entries


# /api/proxy-stream が使う、フォーマットごとのCDN直リンクの短時間キャッシュ
_STREAM_URL_CACHE = {}
STREAM_URL_CACHE_TTL_SEC = int(os.environ.get("YTDLP_API_STREAM_URLCACHE_TTL", "1800"))  # 30分

# ---------- worker / 稼働状況 ----------

SERVER_ID = os.environ.get("YTDLP_API_SERVER_ID", "1")
SERVER_NAME = os.environ.get("YTDLP_API_SERVER_NAME", f"Server {SERVER_ID}")
SERVER_ROLE = os.environ.get("YTDLP_API_ROLE", "primary")  # 複数台構成なら secondary 等を指定
START_TIME = time.time()

# 現在処理中のジョブ: video_id -> {"worker":..., "type":..., "started_at": epoch秒}
_ACTIVE_JOBS = {}
_ACTIVE_JOBS_LOCK = threading.Lock()


@contextlib.contextmanager
def _track_processing(video_id, job_type):
    """info取得・stream解決などの間、処理中一覧に載せておく。"""
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS[video_id] = {
            "worker": SERVER_NAME,
            "type": job_type,
            "started_at": time.time(),
        }
    try:
        yield
    finally:
        with _ACTIVE_JOBS_LOCK:
            _ACTIVE_JOBS.pop(video_id, None)


def _uptime_seconds():
    return round(time.time() - START_TIME, 1)


def _format_uptime(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}時間{m}分{s}秒"


# ---------- 永続DB (SQLite): 一覧用インデックス + レスポンスキャッシュ ----------

CACHE_DB_PATH = os.path.join(TMP_DIR_PATH, "cache.db")
_CACHE_DB_LOCK = threading.Lock()


def _db():
    conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                thumbnail TEXT,
                duration INTEGER,
                uploader TEXT,
                first_seen REAL,
                last_seen REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS response_cache (
                key TEXT PRIMARY KEY,
                kind TEXT,
                video_id TEXT,
                payload TEXT,
                created_at REAL
            )
        """)


_init_db()


def _cache_upsert(video_id, data):
    """一覧表示(/api/cache)用の軽量インデックス。"""
    now = time.time()
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO cache (video_id, title, thumbnail, duration, uploader, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,
                thumbnail=excluded.thumbnail,
                duration=excluded.duration,
                uploader=excluded.uploader,
                last_seen=excluded.last_seen
        """, (
            video_id,
            data.get("title"),
            data.get("thumbnail"),
            data.get("duration"),
            data.get("uploader"),
            now,
            now,
        ))


def _cache_count():
    with _CACHE_DB_LOCK, _db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM cache").fetchone()
        return row["c"] if row else 0


def _json_safe(obj):
    """yt-dlpの情報dictには稀にJSON化できない値が混じるため、文字列化して安全にする。"""
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


def _response_cache_get(key):
    with _CACHE_DB_LOCK, _db() as conn:
        row = conn.execute(
            "SELECT payload, created_at FROM response_cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    age = time.time() - row["created_at"]
    if age > RESPONSE_CACHE_TTL_SECONDS:
        return None
    payload = json.loads(row["payload"])
    payload["_cache"] = {
        "hit": True,
        "age_seconds": round(age, 1),
        "expires_in_seconds": round(RESPONSE_CACHE_TTL_SECONDS - age, 1),
    }
    return payload


def _response_cache_set(key, kind, video_id, payload):
    now = time.time()
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO response_cache (key, kind, video_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload=excluded.payload,
                created_at=excluded.created_at
        """, (key, kind, video_id, json.dumps(payload, ensure_ascii=False), now))
        # ついでに期限切れの古いキャッシュも掃除しておく
        threshold = now - RESPONSE_CACHE_TTL_SECONDS
        conn.execute("DELETE FROM response_cache WHERE created_at < ?", (threshold,))


# ---------- 共通ヘルパー ----------

def _resolve_url(video_id):
    """video_idがURLならデコードしてそのまま使い、そうでなければYouTube動画とみなす。"""
    decoded = urllib.parse.unquote(video_id)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", decoded):
        return decoded
    return f"https://www.youtube.com/watch?v={decoded}"


def _resolve_playlist_url(playlist_id):
    """playlist_idがURLならデコードしてそのまま使い、そうでなければYouTubeのプレイリストとみなす。"""
    decoded = urllib.parse.unquote(playlist_id)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", decoded):
        return decoded
    return f"https://www.youtube.com/playlist?list={decoded}"


def _resolve_channel_url(channel_id):
    """
    channel_idがURLならデコードしてそのまま使う。
    '@handle' 形式、'UCxxxx' 形式のチャンネルID、素のハンドル名のどれでも受け付ける。
    """
    decoded = urllib.parse.unquote(channel_id)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", decoded):
        return decoded
    if decoded.startswith("@"):
        return f"https://www.youtube.com/{decoded}/videos"
    if decoded.startswith("UC") and len(decoded) > 10:
        return f"https://www.youtube.com/channel/{decoded}/videos"
    return f"https://www.youtube.com/@{decoded}/videos"


def _sanitize_id(video_id):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", video_id)[:64]


def _add_lang_params(url):
    """
    YouTubeの生ページ(watch page / trending / channel等)を直接requestsで叩く時用。
    Accept-Languageヘッダだけだと日本語のタイトルなのに英語に自動翻訳されて
    返ってくることがあるため、URLに hl=ja&gl=JP を明示的に付けて抑える。
    """
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query.setdefault("hl", "ja")
    query.setdefault("gl", "JP")
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


class _PageResponse:
    """requestsのResponseっぽい最小限の入れ物(status_code/textだけ)。"""
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def _fetch_page(url, timeout=15):
    """
    YouTubeの生HTMLページを取ってくる。requestsではなくPython標準ライブラリ(urllib)を
    使っている。requests(urllib3)だとブロックされるケースでも、urllibだとTLS/HTTPの
    フィンガープリントが変わって通ることがあるため、生スクレイピング系はこちらに統一した。
    cookies.txtが置いてあれば、そのcookieも一緒に送る(consent回避用のCONSENT/SOCSは常に付与)。
    """
    headers = dict(_PAGE_HEADERS)
    headers["Cookie"] = _cookie_header_string()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if e.fp else b""
    except urllib.error.URLError as e:
        raise ApiError(502, f"failed to fetch page: {e.reason}")
    return _PageResponse(status, raw.decode("utf-8", errors="replace"))


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        # YouTubeは視聴環境の言語設定によっては、オリジナルが日本語のタイトルでも
        # 自動翻訳された英語タイトルを返してくることがある。明示的にjaを指定して抑える。
        #
        # player_clientはあえて指定していない。以前はPO Token方式のためmweb/webに
        # 固定していたが、PO Token用のサーバー(bgutil)を常時起動し続けるのが
        # 運用上つらかったため、cookies.txtによる認証に一本化した。
        # ログイン済みcookieがあれば、yt-dlp標準のクライアント選択で
        # 通常問題なくフォーマット一覧が取得できる。
        "extractor_args": {"youtube": {"lang": ["ja"]}},
        # 2025年後半以降、署名解読(nシグネチャ等)に外部JSランタイムが事実上必須になった。
        # Termuxにはdeno(既定ランタイム)が無いのでnodeを明示的に有効化する。
        # (これはcookies.txt方式でも引き続き必要)
        "js_runtimes": {"node": {}},
        # 署名解読スクリプト本体(EJS)をGitHubから取得することを許可する設定。
        # これが無いと "Signature solving failed" で一部/全部のフォーマットが欠落する。
        "remote_components": ["ejs:github"],
    }
    if os.path.isfile(COOKIES_FILE_PATH):
        opts["cookiefile"] = COOKIES_FILE_PATH
    if extra:
        extra = dict(extra)
        # extractor_argsは単純にdict.updateすると丸ごと上書きされてlang指定が
        # 消えてしまうので(例: コメント取得時のmax_comments指定)、ここだけ個別にマージする。
        extra_extractor_args = extra.pop("extractor_args", None)
        opts.update(extra)
        if extra_extractor_args:
            merged_youtube_args = dict(opts["extractor_args"].get("youtube", {}))
            merged_youtube_args.update(extra_extractor_args.get("youtube", {}))
            opts["extractor_args"] = {"youtube": merged_youtube_args}
    return opts


class ApiError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@app.errorhandler(ApiError)
def _handle_api_error(err):
    return jsonify({"detail": err.message}), err.status_code


def _extract(source_url, extra_opts=None):
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(extra_opts)) as ydl:
            return ydl.extract_info(source_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ApiError(400, f"yt-dlp error: {e}")


def _extract_full(video_id):
    """フォーマットを絞らず全情報(formats一覧込み)を取得しつつ、一覧用インデックスにも書き込む。"""
    source_url = _resolve_url(video_id)
    data = _extract(source_url)
    _cache_upsert(video_id, data)
    return data


def _extract_flat(url, playliststart=None, playlistend=None):
    """
    検索結果・プレイリスト・チャンネルの一覧取得用。動画1本ずつをフル解析しないので高速。
    (各動画の詳細が必要な場合は、返ってきたvideo_idで/api/info/{video_id}を叩く)
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "extract_flat": "in_playlist",
        "extractor_args": {"youtube": {"lang": ["ja"]}},
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if os.path.isfile(COOKIES_FILE_PATH):
        opts["cookiefile"] = COOKIES_FILE_PATH
    if playliststart is not None:
        opts["playliststart"] = playliststart
    if playlistend is not None:
        opts["playlistend"] = playlistend
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ApiError(400, f"yt-dlp error: {e}")


def _slim_entry(e):
    """検索結果/プレイリスト/チャンネル一覧の1件分を整形する共通ヘルパー。"""
    thumbnails = e.get("thumbnails") or []
    thumbnail = thumbnails[-1].get("url") if thumbnails else e.get("thumbnail")
    return {
        "video_id": e.get("id"),
        "title": e.get("title"),
        "url": e.get("url") or e.get("webpage_url"),
        "duration": e.get("duration"),
        "view_count": e.get("view_count"),
        "channel": e.get("channel") or e.get("uploader"),
        "channel_id": e.get("channel_id") or e.get("uploader_id"),
        "thumbnail": thumbnail,
        "live_status": e.get("live_status"),
        "upload_date": e.get("upload_date"),
    }



# ---------- health ----------

@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "uptime_seconds": _uptime_seconds()})


# ---------- /api (一覧・説明・実行テストページ) ----------

_API_DOCS_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ytdlp_api - API一覧</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; padding: 24px; background: #0d1117; color: #e6edf3;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #9aa7b2; font-size: 13px; margin-bottom: 20px; }
  .sub a { color: #58a6ff; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 16px 18px; margin-bottom: 14px;
  }
  .card-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .method {
    font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 5px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .method-GET { background: #123a2b; color: #3fb950; }
  .method-POST { background: #1c2b4a; color: #58a6ff; }
  .method-DELETE { background: #401e22; color: #f85149; }
  .path {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 14px; color: #e6edf3;
  }
  .desc { font-size: 13px; color: #9aa7b2; margin: 8px 0 12px; }
  .params { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
  .params label { font-size: 12px; color: #9aa7b2; display: flex; flex-direction: column; gap: 4px; }
  .params input {
    background: #0d1117; border: 1px solid #30363d; color: #e6edf3;
    border-radius: 6px; padding: 6px 8px; font-size: 13px; min-width: 160px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  button.run {
    background: #238636; color: white; border: none; border-radius: 6px;
    padding: 7px 14px; font-size: 13px; cursor: pointer; font-weight: 600;
  }
  button.run:hover { background: #2ea043; }
  button.run:disabled { background: #30363d; cursor: default; }
  .result {
    margin-top: 10px; background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 10px 12px; font-size: 12px; max-height: 320px; overflow: auto;
    white-space: pre-wrap; word-break: break-all;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; display: none;
  }
  .result.show { display: block; }
  .status-ok { color: #3fb950; }
  .status-err { color: #f85149; }
  .meta-line { color: #6e7681; margin-bottom: 6px; }
</style>
</head>
<body>
  <h1>ytdlp_api</h1>
  <div class="sub">yt-dlp powered API (no UI, docs only) &middot; <a href="/api/stats">/api/stats ダッシュボードはこちら</a></div>

  <div id="cards"></div>

<script>
const ENDPOINTS = [
  { method: "GET", path: "/api/health", desc: "死活監視", params: [] },
  { method: "GET", path: "/api/search", desc: "YouTube検索(flat抽出で高速)。",
    params: [
      { name: "q", in: "query", placeholder: "アイドルマスター" },
      { name: "limit", in: "query", placeholder: "20" },
    ] },
  { method: "GET", path: "/api/playlist/{playlist_id}", desc: "プレイリストのメタ情報+収録動画一覧。",
    params: [
      { name: "playlist_id", in: "path", placeholder: "PLxxxxxxxxxxxxxxxx" },
      { name: "limit", in: "query", placeholder: "100" },
      { name: "offset", in: "query", placeholder: "0" },
    ] },
  { method: "GET", path: "/api/channel/{channel_id}", desc: "チャンネルのメタ情報+投稿動画一覧+アバター/バナー(base64)。@handle/UCxxxx/フルURLいずれも可。",
    params: [
      { name: "channel_id", in: "path", placeholder: "@handle" },
      { name: "limit", in: "query", placeholder: "50" },
      { name: "offset", in: "query", placeholder: "0" },
    ] },
  { method: "GET", path: "/api/comments/{video_id}", desc: "動画のコメント一覧を取得。件数が多いと時間がかかる。",
    params: [
      { name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" },
      { name: "limit", in: "query", placeholder: "50" },
    ] },
  { method: "GET", path: "/api/related/{video_id}", desc: "関連動画。watch pageのytInitialDataを構造非依存で探索して取得(YouTube側の多少の変更には自動追従)。",
    params: [
      { name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" },
      { name: "limit", in: "query", placeholder: "10" },
    ] },
  { method: "GET", path: "/api/trending", desc: "おすすめ/トレンドフィード(非ログイン・非パーソナライズ)。トップページ表示用。",
    params: [
      { name: "limit", in: "query", placeholder: "24" },
    ] },
  { method: "GET", path: "/api/info/{video_id}", desc: "動画の全メタデータを取得(ストリームURLは含まない)。7時間キャッシュ。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
  { method: "GET", path: "/api/stream/{video_id}", desc: "その動画の全ストリームURL一覧を取得。HLS(m3u8)直リンクがあればhls_urlに入る。7時間キャッシュ。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
  { method: "GET", path: "/api/stats/data", desc: "worker一覧・処理中一覧・キャッシュ件数・稼働時間をJSONで取得。", params: [] },
  { method: "GET", path: "/api/workers", desc: "このサーバー(worker)の情報一覧。", params: [] },
  { method: "GET", path: "/api/processing", desc: "現在処理中のvideo_id一覧(経過時間つき)。", params: [] },
  { method: "GET", path: "/api/cache", desc: "これまでに解決した動画の一覧。検索・件数指定が可能。",
    params: [
      { name: "q", in: "query", placeholder: "(検索語、省略可)" },
      { name: "limit", in: "query", placeholder: "50" },
      { name: "offset", in: "query", placeholder: "0" },
    ] },
  { method: "GET", path: "/api/cache/{video_id}", desc: "キャッシュ済み単一動画の情報を取得。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
  { method: "DELETE", path: "/api/cache/{video_id}", desc: "一覧インデックス+レスポンスキャッシュを削除する。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
  { method: "DELETE", path: "/api/cache", desc: "キャッシュを全部削除する(強制リフレッシュ用)。",
    params: [] },
];

function buildCard(ep, index) {
  const card = document.createElement("div");
  card.className = "card";

  const head = document.createElement("div");
  head.className = "card-head";
  head.innerHTML = `<span class="method method-${ep.method}">${ep.method}</span><span class="path">${ep.path}</span>`;
  card.appendChild(head);

  const desc = document.createElement("div");
  desc.className = "desc";
  desc.textContent = ep.desc;
  card.appendChild(desc);

  const paramsRow = document.createElement("div");
  paramsRow.className = "params";
  const inputs = {};
  ep.params.forEach(p => {
    const label = document.createElement("label");
    label.textContent = `${p.name} (${p.in})`;
    const input = document.createElement("input");
    input.placeholder = p.placeholder || "";
    input.dataset.name = p.name;
    input.dataset.in = p.in;
    inputs[p.name] = input;
    label.appendChild(input);
    paramsRow.appendChild(label);
  });
  card.appendChild(paramsRow);

  const btn = document.createElement("button");
  btn.className = "run";
  btn.textContent = "実行";
  card.appendChild(btn);

  const result = document.createElement("div");
  result.className = "result";
  card.appendChild(result);

  btn.addEventListener("click", async () => {
    let path = ep.path;
    const query = new URLSearchParams();
    for (const p of ep.params) {
      const val = inputs[p.name].value.trim();
      if (p.in === "path") {
        path = path.replace(`{${p.name}}`, encodeURIComponent(val || p.placeholder || ""));
      } else if (p.in === "query" && val) {
        query.set(p.name, val);
      }
    }
    const qs = query.toString();
    const url = path + (qs ? `?${qs}` : "");

    btn.disabled = true;
    btn.textContent = "実行中...";
    result.classList.add("show");
    result.textContent = `${ep.method} ${url}\\n...`;

    const startedAt = performance.now();
    try {
      const res = await fetch(url, { method: ep.method });
      const elapsed = Math.round(performance.now() - startedAt);
      const text = await res.text();
      let pretty = text;
      try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (e) {}
      const statusClass = res.ok ? "status-ok" : "status-err";
      result.innerHTML =
        `<div class="meta-line">${ep.method} ${url}</div>` +
        `<div class="meta-line"><span class="${statusClass}">HTTP ${res.status}</span> &middot; ${elapsed}ms</div>` +
        `<div>${pretty.replace(/</g, "&lt;")}</div>`;
    } catch (e) {
      result.innerHTML = `<div class="meta-line">${ep.method} ${url}</div><div class="status-err">エラー: ${e}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "実行";
    }
  });

  return card;
}

const container = document.getElementById("cards");
ENDPOINTS.forEach((ep, i) => container.appendChild(buildCard(ep, i)));
</script>
</body>
</html>
"""


@app.get("/api")
def api_docs():
    return Response(_API_DOCS_HTML, mimetype="text/html")


# ---------- workers / 処理中 / stats ----------

def _workers_snapshot():
    with _ACTIVE_JOBS_LOCK:
        processing_count = len(_ACTIVE_JOBS)
    return [{
        "server_id": SERVER_ID,
        "worker": SERVER_NAME,
        "processing": processing_count,
        "role": SERVER_ROLE,
    }]


def _processing_snapshot():
    now = time.time()
    with _ACTIVE_JOBS_LOCK:
        items = list(_ACTIVE_JOBS.items())
    result = []
    for video_id, job in items:
        started_dt = datetime.fromtimestamp(job["started_at"]).astimezone()
        result.append({
            "video_id": video_id,
            "worker": job["worker"],
            "type": job["type"],
            "elapsed_seconds": round(now - job["started_at"], 1),
            "started_at": started_dt.isoformat(timespec="seconds"),
        })
    result.sort(key=lambda x: x["elapsed_seconds"])
    return result


def _stats_snapshot():
    return {
        "workers": _workers_snapshot(),
        "processing": _processing_snapshot(),
        "cache_count": _cache_count(),
        "uptime_seconds": _uptime_seconds(),
        "uptime_human": _format_uptime(_uptime_seconds()),
    }


@app.get("/api/workers")
def workers():
    return jsonify(_workers_snapshot())


@app.get("/api/processing")
def processing():
    return jsonify(_processing_snapshot())


@app.get("/api/stats/data")
def stats_data():
    return jsonify(_stats_snapshot())


_STATS_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ytdlp_api - stats</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; padding: 24px; background: #0d1117; color: #e6edf3;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  h1 { font-size: 20px; margin: 0 0 16px; }
  h2 { font-size: 15px; color: #9aa7b2; margin: 28px 0 8px; }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 14px 18px; min-width: 130px;
  }
  .card .label { font-size: 12px; color: #9aa7b2; }
  .card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 10px 14px; font-size: 13px; }
  th { color: #9aa7b2; font-weight: 600; border-bottom: 1px solid #30363d; }
  td { border-bottom: 1px solid #21262d; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  tr:last-child td { border-bottom: none; }
  .role-primary { color: #58a6ff; }
  .empty { color: #6e7681; font-size: 13px; padding: 12px 14px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#3fb950; margin-right:6px; }
</style>
</head>
<body>
  <h1><span class="dot"></span>ytdlp_api stats</h1>

  <div class="cards">
    <div class="card"><div class="label">稼働時間</div><div class="value" id="uptime">-</div></div>
    <div class="card"><div class="label">キャッシュ件数</div><div class="value" id="cacheCount">-</div></div>
    <div class="card"><div class="label">Worker数</div><div class="value" id="workerCount">-</div></div>
    <div class="card"><div class="label">処理中件数</div><div class="value" id="processingCount">-</div></div>
  </div>

  <h2>Workers</h2>
  <table>
    <thead><tr><th>Server ID</th><th>Worker</th><th>処理中</th><th>役割</th></tr></thead>
    <tbody id="workersBody"></tbody>
  </table>

  <h2>処理中ID</h2>
  <table>
    <thead><tr><th>Video ID</th><th>Worker</th><th>種別</th><th>経過</th><th>開始</th></tr></thead>
    <tbody id="processingBody"></tbody>
  </table>

<script>
async function refresh() {
  try {
    const res = await fetch('/api/stats/data');
    const data = await res.json();

    document.getElementById('uptime').textContent = data.uptime_human;
    document.getElementById('cacheCount').textContent = data.cache_count;
    document.getElementById('workerCount').textContent = data.workers.length;
    document.getElementById('processingCount').textContent = data.processing.length;

    const workersBody = document.getElementById('workersBody');
    workersBody.innerHTML = data.workers.map(w => `
      <tr>
        <td>${w.server_id}</td>
        <td>${w.worker}</td>
        <td>${w.processing}</td>
        <td class="${w.role === 'primary' ? 'role-primary' : ''}">${w.role}</td>
      </tr>
    `).join('');

    const processingBody = document.getElementById('processingBody');
    processingBody.innerHTML = data.processing.length
      ? data.processing.map(p => `
        <tr>
          <td>${p.video_id}</td>
          <td>${p.worker}</td>
          <td>${p.type}</td>
          <td>${p.elapsed_seconds}s</td>
          <td>${p.started_at}</td>
        </tr>
      `).join('')
      : '<tr><td colspan="5" class="empty">現在処理中のジョブはありません</td></tr>';
  } catch (e) {
    console.error(e);
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@app.get("/api/stats")
def stats_page():
    return Response(_STATS_HTML, mimetype="text/html")


# ---------- cache (これまでに解決した動画の一覧インデックス) ----------

@app.get("/api/cache")
def cache_list():
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    offset = max(0, int(request.args.get("offset", 0)))
    q = request.args.get("q")

    with _CACHE_DB_LOCK, _db() as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                """SELECT * FROM cache WHERE video_id LIKE ? OR title LIKE ?
                   ORDER BY last_seen DESC LIMIT ? OFFSET ?""",
                (like, like, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM cache WHERE video_id LIKE ? OR title LIKE ?",
                (like, like),
            ).fetchone()["c"]
        else:
            rows = conn.execute(
                "SELECT * FROM cache ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS c FROM cache").fetchone()["c"]

    items = [{
        "video_id": r["video_id"],
        "title": r["title"],
        "thumbnail": r["thumbnail"],
        "duration": r["duration"],
        "uploader": r["uploader"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
    } for r in rows]

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": items})


@app.get("/api/cache/<video_id>")
def cache_get(video_id):
    with _CACHE_DB_LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM cache WHERE video_id = ?", (video_id,)).fetchone()
    if not row:
        raise ApiError(404, "not in cache")
    return jsonify({
        "video_id": row["video_id"],
        "title": row["title"],
        "thumbnail": row["thumbnail"],
        "duration": row["duration"],
        "uploader": row["uploader"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    })


@app.delete("/api/cache/<video_id>")
def cache_delete(video_id):
    _require_admin_password()
    with _CACHE_DB_LOCK, _db() as conn:
        cur = conn.execute("DELETE FROM cache WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM response_cache WHERE video_id = ?", (video_id,))
    if cur.rowcount == 0:
        raise ApiError(404, "not in cache")
    return jsonify({"deleted": video_id})


@app.delete("/api/cache")
def cache_delete_all():
    """一覧インデックス・レスポンスキャッシュ(7時間キャッシュ)を全部消す。
    間違ったデータがキャッシュされてしまった時の強制リフレッシュ用。
    ?password= がYTDLP_API_ADMIN_PASSWORDと一致しないと実行できない。"""
    _require_admin_password()
    with _CACHE_DB_LOCK, _db() as conn:
        index_count = conn.execute("SELECT COUNT(*) AS c FROM cache").fetchone()["c"]
        response_count = conn.execute("SELECT COUNT(*) AS c FROM response_cache").fetchone()["c"]
        conn.execute("DELETE FROM cache")
        conn.execute("DELETE FROM response_cache")
    # 短時間キャッシュ(URL解決結果)もついでに消しておく
    _STREAM_URL_CACHE.clear()
    return jsonify({
        "deleted": "all",
        "index_entries_removed": index_count,
        "response_cache_entries_removed": response_count,
    })


# ---------- search / playlist / channel / comments / related ----------

@app.get("/api/search")
def search():
    """
    ?q=検索語 でYouTube検索。

    まずYouTubeの検索結果ページを直接スクレイピングする(related/trendingと同じ
    「動画カードを総当たりで拾う」方式)。こちらだと各結果にチャンネルの小さい
    アイコン画像(channel_thumbnail)も付いてくる。yt-dlpのflat検索(ytsearchN:)には
    このアイコン情報が無いため、フォールバック用にとどめている。
    """
    q = request.args.get("q")
    if not q:
        raise ApiError(400, "query parameter 'q' is required")
    limit = max(1, min(int(request.args.get("limit", 20)), 50))

    key = f"search:{q}:{limit}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    entries = None
    with _track_processing(f"search:{q}", "search"):
        search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
        try:
            resp = _fetch_page(_add_lang_params(search_url), timeout=15)
            if resp.status_code < 400:
                yt_data = _extract_yt_initial_data(resp.text)
                if yt_data:
                    entries = _parse_video_cards(yt_data, None, limit)
        except ApiError:
            pass

        if not entries:
            # スクレイピングがダメだった場合のフォールバック(チャンネルアイコンは付かない)
            info = _extract_flat(f"ytsearch{limit}:{q}")
            entries = [_slim_entry(e) for e in (info.get("entries") or [])]

    result = _json_safe({
        "query": q,
        "result_count": len(entries),
        "entries": entries,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "search", q, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/playlist/<playlist_id>")
def playlist(playlist_id):
    """プレイリストのメタ情報+収録動画一覧(flat抽出)。?limit=&offset=で範囲指定。"""
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    offset = max(0, int(request.args.get("offset", 0)))

    key = f"playlist:{playlist_id}:{limit}:{offset}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    url = _resolve_playlist_url(playlist_id)
    with _track_processing(playlist_id, "playlist"):
        info = _extract_flat(url, playliststart=offset + 1, playlistend=offset + limit)

    entries = [_slim_entry(e) for e in (info.get("entries") or [])]
    result = _json_safe({
        "playlist_id": info.get("id") or playlist_id,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "channel_id": info.get("channel_id") or info.get("uploader_id"),
        "webpage_url": info.get("webpage_url"),
        "description": info.get("description"),
        "entry_count_total": info.get("playlist_count"),
        "entry_count_returned": len(entries),
        "entries": entries,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "playlist", playlist_id, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/channel/<channel_id>")
def channel(channel_id):
    """
    チャンネルのメタ情報+投稿動画一覧(flat抽出)。?limit=&offset=で範囲指定。
    channel_id は '@handle'、'UCxxxx'、素のハンドル名、フルURLのいずれでも指定可能。

    アバター/バナーはyt-dlpのextract_flatだけでは取れない(特にバナー)ため、
    チャンネルページのytInitialDataを別途取得してrelated/trendingと同じ要領で
    "avatar" / "banner" というキーを持つノードを総当たりで探している。
    見つかった画像はサーバー側で取得してbase64のdata URIにしてから返す
    (ホットリンク周りの問題を避けるため。?base64=0を付けると元のURLのままにできる)。
    """
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    offset = max(0, int(request.args.get("offset", 0)))
    want_base64 = request.args.get("base64", "1") != "0"

    key = f"channel:{channel_id}:{limit}:{offset}:{want_base64}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    url = _resolve_channel_url(channel_id)
    with _track_processing(channel_id, "channel"):
        info = _extract_flat(url, playliststart=offset + 1, playlistend=offset + limit)

        avatar_url = None
        banner_url = None
        page_html = None
        try:
            page_resp = _fetch_page(_add_lang_params(url), timeout=10)
            if page_resp.status_code < 400:
                page_html = page_resp.text
        except ApiError:
            pass

        if page_html:
            # og:imageメタタグが一番単純で壊れにくいので最優先で使う
            avatar_url = _extract_og_image(page_html)
            yt_data = _extract_yt_initial_data(page_html)
            if yt_data:
                if not avatar_url:
                    # channelMetadataRendererを狙い撃ち(汎用の"avatar"総当たりより確実)
                    avatar_url = _find_channel_metadata_avatar(yt_data)
                if not avatar_url:
                    avatar_url = _find_named_image_url(yt_data, "avatar")
                banner_url = _find_named_image_url(yt_data, "banner")

        if not avatar_url:
            # それでもダメならyt-dlp側のthumbnailsにフォールバック
            thumbs = info.get("thumbnails") or []
            if thumbs:
                avatar_url = thumbs[-1].get("url")

        avatar_b64 = _image_to_data_uri(avatar_url) if want_base64 else None
        banner_b64 = _image_to_data_uri(banner_url) if want_base64 else None

    entries = [_slim_entry(e) for e in (info.get("entries") or [])]
    result = _json_safe({
        "channel_id": info.get("channel_id") or info.get("id") or channel_id,
        "channel": info.get("channel") or info.get("uploader") or info.get("title"),
        "channel_follower_count": info.get("channel_follower_count"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "avatar": avatar_url,
        "avatar_base64": avatar_b64,
        "banner": banner_url,
        "banner_base64": banner_b64,
        "entry_count_total": info.get("playlist_count"),
        "entry_count_returned": len(entries),
        "entries": entries,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "channel", channel_id, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/comments/<video_id>")
def comments(video_id):
    """動画のコメント一覧を取得する(yt-dlpのgetcomments機能)。件数が多いと時間がかかる。"""
    limit = str(max(1, min(int(request.args.get("limit", 50)), 500)))

    key = f"comments:{video_id}:{limit}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    source_url = _resolve_url(video_id)
    with _track_processing(video_id, "comments"):
        data = _extract(source_url, {
            "getcomments": True,
            "extractor_args": {"youtube": {"max_comments": [limit, "all", limit, "10"]}},
        })

    raw_comments = data.get("comments") or []
    slim_comments = [{
        "id": c.get("id"),
        "text": c.get("text"),
        "author": c.get("author"),
        "author_id": c.get("author_id"),
        "author_thumbnail": c.get("author_thumbnail"),
        "author_is_uploader": c.get("author_is_uploader"),
        "like_count": c.get("like_count"),
        "is_pinned": c.get("is_pinned"),
        "is_favorited": c.get("is_favorited"),
        "parent": c.get("parent"),
        "timestamp": c.get("timestamp"),
        "time_text": c.get("time_text"),
    } for c in raw_comments]

    result = _json_safe({
        "video_id": video_id,
        "comment_count_returned": len(slim_comments),
        "comment_count_total": data.get("comment_count"),
        "comments": slim_comments,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "comments", video_id, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/livechat/<video_id>")
def livechat(video_id):
    """
    ライブ配信(または過去のライブ配信のアーカイブ)のチャットを取得する。

    【試験的な実装であることに注意】
    YouTubeのライブチャットは本来「継続トークン」を辿りながら少しずつ取得する
    仕組みになっていて、配信全体のチャットを遡って全部取るには何度もリクエストを
    繰り返す必要がある。この実装はそこまでは行っておらず、yt-dlpが最初に教えてくれる
    チャットデータのURLに一度アクセスして、そこに含まれているメッセージだけを
    パースして返す簡易版。ライブチャットが存在しない動画(通常のアップロード動画等)では
    404になる。
    """
    limit = max(1, min(int(request.args.get("limit", 200)), 500))

    key = f"livechat:{video_id}:{limit}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    source_url = _resolve_url(video_id)
    with _track_processing(video_id, "livechat"):
        data = _extract(source_url)
        live_chat_formats = (data.get("subtitles") or {}).get("live_chat") or []
        if not live_chat_formats:
            raise ApiError(404, "this video has no live chat available (not a livestream, or chat replay is disabled)")

        chat_url = live_chat_formats[0].get("url")
        if not chat_url:
            raise ApiError(404, "live chat url not found")

        resp = _fetch_page(chat_url, timeout=15)
        if resp.status_code >= 400:
            raise ApiError(502, f"failed to fetch live chat data: HTTP {resp.status_code}")

    # ライブチャットのレスポンスは1行1JSONのことが多いので、行ごとにパースを試す
    messages = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        for node in _walk(chunk):
            if not isinstance(node, dict) or "message" not in node:
                continue
            if "authorName" not in node and "authorExternalChannelId" not in node:
                continue
            text = _runs_text(node.get("message"))
            if not text:
                continue
            messages.append({
                "author": _runs_text(node.get("authorName")),
                "text": text,
                "timestamp_usec": node.get("timestampUsec"),
            })
            if len(messages) >= limit:
                break
        if len(messages) >= limit:
            break

    result = _json_safe({
        "video_id": video_id,
        "method": "experimental_live_chat_first_segment",
        "note": "継続トークンを辿る本格実装ではなく、最初に取得できた範囲のチャットだけを返す試験的な機能です。",
        "message_count": len(messages),
        "messages": messages,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "livechat", video_id, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


def _find_balanced_json(text, start_idx):
    # start_idxの '{' から対応する '}' までを文字列として切り出すだけの地味な関数。
    # 中に文字列リテラルが混ざってるとナイーブな正規表現だと簡単に壊れるので、
    # ちゃんと文字列/エスケープを見ながら深さを数える。
    depth = 0
    in_string = False
    escape = False
    i = start_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx:i + 1]
        i += 1
    return None


def _extract_yt_initial_data(html):
    # 動画ページのソースには <script>var ytInitialData = {...};</script> みたいな形で
    # ページの中身が丸ごとJSONで埋まってる。そこを引っこ抜くだけ。
    # マーカーを何パターンか用意してるのは、YouTubeが時々書き方を変えてくるため
    # (var付き/なし、ブラケット記法、など)。
    for marker in ("var ytInitialData = ", 'ytInitialData"] = ', "ytInitialData = "):
        idx = html.find(marker)
        if idx == -1:
            continue
        brace_idx = html.find("{", idx)
        if brace_idx == -1:
            continue
        json_str = _find_balanced_json(html, brace_idx)
        if not json_str:
            continue
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            continue
    return None


def _runs_text(node):
    # YouTubeの内部JSONはテキストが {"simpleText": "..."} だったり
    # {"runs": [{"text": "a"}, {"text": "b"}]} だったりバラバラなので吸収する。
    if not node:
        return None
    if "simpleText" in node:
        return node["simpleText"]
    runs = node.get("runs") or []
    text = "".join(r.get("text", "") for r in runs)
    return text or None


def _dig(obj, *path):
    # ネストしたdict/listを obj["a"]["b"][0]["c"] みたいに辿るやつ。
    # 途中でキーが無くても例外で落ちずにNoneを返すだけの雑なヘルパー。
    cur = obj
    for key in path:
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def _walk(node):
    # JSONツリー全体を舐めて、出てきたdictを片っ端からyieldする。
    # 「compactVideoRendererはこの階層のこのキーの下」みたいな決め打ちを
    # やめて全部潜るようにしておくと、YouTubeが階層をちょっといじった程度では
    # 壊れなくなる。多少非効率だが動画1本分のJSONくらいなら誤差。
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _find_thumbnails_under(node):
    # dictの中のどこかにある "thumbnails": [...] を深さ問わず探して返す。
    # YouTubeは新しいページ構造(pageHeaderViewModel系)だと同じ役割のキーが
    # "sources" という名前になっていることがあるので、そちらも見る。
    # avatar/bannerのJSON構造はチャンネルによって微妙に階層が違うことがあるので、
    # ピンポイントでパスを決め打ちせずに総当たりする。
    if isinstance(node, dict):
        for key in ("thumbnails", "sources"):
            thumbs = node.get(key)
            if isinstance(thumbs, list) and thumbs and isinstance(thumbs[0], dict) and thumbs[0].get("url"):
                return thumbs
        for v in node.values():
            found = _find_thumbnails_under(v)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_thumbnails_under(item)
            if found:
                return found
    return None


def _find_channel_metadata_avatar(yt_data):
    """
    チャンネルページのアバターを狙い撃ちする。"channelMetadataRenderer" は
    YouTubeのチャンネルページで昔から安定してavatar/description/keywords等を
    まとめて持っているキーなので、og:imageや汎用の"avatar"総当たりより確実なことが多い。
    """
    for node in _walk(yt_data):
        if "channelMetadataRenderer" in node:
            meta = node["channelMetadataRenderer"]
            thumbs = _find_thumbnails_under(meta.get("avatar") or meta)
            if thumbs:
                return thumbs[-1].get("url")
    return None


def _find_video_owner_avatar(yt_data):
    """
    watch pageのytInitialDataには"avatar"というキーがコメント投稿者や関連動画の
    チャンネルなど何人分も含まれていて、単純に最初に見つかったものを使うと
    別人のアイコンを拾ってしまうことがある。動画の投稿者情報は
    "videoOwnerRenderer" というキーの下にまとまっているので、そこだけを狙って
    アバター画像を探す(総当たりよりずっとピンポイント)。
    """
    for node in _walk(yt_data):
        if "videoOwnerRenderer" in node:
            owner = node["videoOwnerRenderer"]
            thumbs = _find_thumbnails_under(owner.get("thumbnail") or owner)
            if thumbs:
                return thumbs[-1].get("url")
    return None


def _find_named_image_url(yt_data, key_name):
    """ytInitialDataの中から、例えば "avatar" や "banner" というキーを持つノードを探し、
    その中に埋まっているサムネイル一覧の一番大きい画像URLを返す。見つからなければNone。"""
    for node in _walk(yt_data):
        if key_name in node:
            thumbs = _find_thumbnails_under(node[key_name])
            if thumbs:
                return thumbs[-1].get("url")
    return None


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE
)


def _extract_og_image(html):
    """
    ページのHTMLに埋まっている <meta property="og:image" content="..."> からURLを拾う。
    ytInitialDataの中を総当たりで探すより単純で、YouTube側のページ構造変更の影響を
    受けにくい。チャンネルページのog:imageはたいていアバター画像になっている。
    """
    m = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE_ALT.search(html)
    return m.group(1) if m else None


def _image_to_data_uri(url):
    """画像URLをサーバー側で取得してbase64のdata URIにして返す(ホットリンク周りの問題を避けるため)。
    取得に失敗したらNoneを返すだけで、呼び出し側は気にせずそのまま使える。"""
    if not url:
        return None
    try:
        resp = _http.get(url, timeout=10)
    except requests.RequestException:
        return None
    if resp.status_code >= 400 or not resp.content:
        return None
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    b64 = base64.b64encode(resp.content).decode("ascii")
    return f"data:{content_type};base64,{b64}"


# 動画レンダラーからチャンネルIDを拾うための候補パス。
# レンダラーの種類(compactVideoRenderer / videoRenderer / gridVideoRenderer 等)によって
# 微妙に構造が違うので、上から順に試して最初に見つかったものを使う。
_CHANNEL_ID_PATHS = (
    ("channelThumbnailSupportedRenderers", "channelThumbnailWithLinkRenderer",
     "navigationEndpoint", "browseEndpoint", "browseId"),
    ("shortBylineText", "runs", 0, "navigationEndpoint", "browseEndpoint", "browseId"),
    ("longBylineText", "runs", 0, "navigationEndpoint", "browseEndpoint", "browseId"),
)

# 検索結果/関連動画/トレンドのカードに、投稿者の小さいアイコン画像が埋まっていることがある。
# チャンネルIDと同じ場所(channelThumbnailSupportedRenderers)にぶら下がっていることが多い。
_CHANNEL_THUMBNAIL_PATHS = (
    ("channelThumbnailSupportedRenderers", "channelThumbnailWithLinkRenderer", "thumbnail", "thumbnails"),
    ("channelThumbnail", "thumbnails"),
)


def _looks_like_video(node):
    # 「これは動画1本を表すレンダラーっぽいか」の判定。
    # レンダラー名(compactVideoRendererとか)には依存せず、中身の形だけで判断する。
    # videoIdとtitleは必須、あとthumbnail/lengthText/viewCount系のどれか1つでもあれば
    # だいたい本物の動画カード。これで検索候補やチャットのメンションなど
    # videoIdだけ持ってる別種のノイズを弾ける。
    if not isinstance(node, dict):
        return False
    if not node.get("videoId") or not node.get("title"):
        return False
    return bool(
        node.get("thumbnail")
        or node.get("lengthText")
        or node.get("shortViewCountText")
        or node.get("viewCountText")
    )


def _parse_video_cards(yt_data, exclude_id, limit):
    """
    ytInitialDataの中から動画カードっぽいノードを片っ端から拾ってリストにする。
    決め打ちのJSONパスに頼らないので、YouTube側が階層構造を変えてきても
    (レンダラー自体の形が大きく変わらない限り)そのまま動く。
    関連動画(watch pageのsecondaryResults)にもトレンド(トップページのrichGrid)にも
    そのまま使い回せる。
    """
    seen = set()
    entries = []

    for node in _walk(yt_data):
        if not _looks_like_video(node):
            continue

        video_id = node["videoId"]
        if video_id == exclude_id or video_id in seen:
            continue
        seen.add(video_id)

        channel_id = None
        for path in _CHANNEL_ID_PATHS:
            channel_id = _dig(node, *path)
            if channel_id:
                break

        channel_thumbnail = None
        for path in _CHANNEL_THUMBNAIL_PATHS:
            thumbs = _dig(node, *path)
            if thumbs:
                channel_thumbnail = thumbs[-1].get("url")
                break

        thumbnails = _dig(node, "thumbnail", "thumbnails") or []

        entries.append({
            "video_id": video_id,
            "title": _runs_text(node.get("title")),
            "channel": _runs_text(node.get("longBylineText") or node.get("shortBylineText")),
            "channel_id": channel_id,
            "channel_thumbnail": channel_thumbnail,
            "length_text": _dig(node, "lengthText", "simpleText"),
            "view_count_text": _dig(node, "shortViewCountText", "simpleText") or _runs_text(node.get("viewCountText")),
            "thumbnail": thumbnails[-1]["url"] if thumbnails else None,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })

        if len(entries) >= limit:
            break

    return entries


# watch pageを叩くだけなのに毎回新規コネクション張るのは無駄なので使い回す。
# ついでにブラウザっぽいヘッダを一式乗せておく(素のrequestsのUAだと弾かれることがある)。
_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
})
# Cookie無しでYouTubeに直接アクセスすると、地域によっては本来のページの代わりに
# 「続行する前に」のクッキー同意画面が返ってきて中身が空になることがある。
# この同意済みクッキーを最初から持たせておくことでその画面を回避する。
_http.cookies.set("CONSENT", "YES+1", domain=".youtube.com")
_http.cookies.set("SOCS", "CAI", domain=".youtube.com")

# cookies.txtがあれば、こちらのセッション(画像取得等に使う)にも反映しておく。
_cookiejar_at_startup = _load_cookiejar()
if _cookiejar_at_startup:
    for _cookie in _cookiejar_at_startup:
        _http.cookies.set(_cookie.name, _cookie.value, domain=_cookie.domain or ".youtube.com")


@app.get("/api/related/<video_id>")
def related(video_id):
    """
    関連動画。yt-dlp自体には関連動画を取ってくる機能が無いので、watch pageのHTMLに
    埋め込まれているytInitialDataを自前で解析している。
    パースは決め打ちのJSONパスではなく「動画カードっぽい形のノードを全部拾う」方式にしてあるので、
    YouTubeがページ構造を多少いじってきても大体は追従できるはず(レンダラーの形自体が
    大きく変わったら流石にダメだが、その時はまたどこかで直す)。
    """
    limit = max(1, min(int(request.args.get("limit", 10)), 50))

    key = f"related:{video_id}:{limit}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    watch_url = _resolve_url(video_id)
    with _track_processing(video_id, "related"):
        resp = _fetch_page(_add_lang_params(watch_url), timeout=15)
        if resp.status_code >= 400:
            raise ApiError(502, f"watch page returned HTTP {resp.status_code}")
        yt_data = _extract_yt_initial_data(resp.text)

    if yt_data is None:
        raise ApiError(
            502,
            "failed to parse ytInitialData from the watch page "
            "(YouTube may have changed its page structure, or this isn't a YouTube video)",
        )

    entries = _parse_video_cards(yt_data, video_id, limit)

    result = _json_safe({
        "video_id": video_id,
        "method": "youtube_watch_page_scrape",
        "note": "watch pageのytInitialDataから抽出した関連動画。非公式な取得方法。",
        "entry_count": len(entries),
        "entries": entries,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "related", video_id, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/trending")
def trending():
    """
    おすすめ/トレンドフィード。

    YouTube公式のトレンドページのスクレイピングは不安定だったため、代わりに
    「このAPI経由で実際に視聴された動画」の集計をトレンドとして返す方式に変更した。
    誰もまだ見ていない状態ではentriesが空になる(YouTube本家のトレンドとは無関係)。
    """
    limit = max(1, min(int(request.args.get("limit", 24)), 100))

    with _track_processing("trending", "trending"):
        entries = _get_local_trending(limit)

    return jsonify(_json_safe({
        "method": "local_usage_based",
        "note": (
            "YouTube公式のトレンドではなく、このサイトを経由して実際に視聴された動画を"
            "視聴回数順に並べたものです。まだ誰も見ていない動画は出てきません。"
        ),
        "entry_count": len(entries),
        "entries": entries,
    }))


# ---------- info (ストリームURLは含まない全メタデータ) ----------

# レスポンスから除外するキー(ストリームURLや巨大すぎる/内部利用のフィールド)
_INFO_BLACKLIST_KEYS = {
    "formats", "requested_formats", "requested_downloads",
    "url", "manifest_url", "http_headers",
    "requested_subtitles", "automatic_captions", "subtitles",
    "cookies", "_format_sort_fields", "format_sort", "downloader_options",
}


def _pick_reference_format(data):
    """ストリームURLを含めずに、解像度/FPS/ファイルサイズの参考値だけを拾う。"""
    formats = data.get("formats") or []
    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none")]
    pool = video_formats or formats
    if not pool:
        return {}
    best = max(pool, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return {
        "format_id": best.get("format_id"),
        "ext": best.get("ext"),
        "width": best.get("width"),
        "height": best.get("height"),
        "fps": best.get("fps"),
        "filesize": best.get("filesize"),
        "filesize_approx": best.get("filesize_approx"),
        "tbr": best.get("tbr"),
        "vcodec": best.get("vcodec"),
        "acodec": best.get("acodec"),
    }


def _build_info_payload(data):
    ref = _pick_reference_format(data)

    sanitized = {k: v for k, v in data.items() if k not in _INFO_BLACKLIST_KEYS}
    sanitized["subtitles_languages"] = sorted((data.get("subtitles") or {}).keys())
    sanitized["automatic_captions_languages"] = sorted((data.get("automatic_captions") or {}).keys())
    sanitized["resolution_reference"] = ref  # ストリームURLは含まない参考値(解像度/FPS/ファイルサイズ)
    sanitized["cache_ttl_seconds"] = RESPONSE_CACHE_TTL_SECONDS

    return _json_safe(sanitized)


@app.get("/api/info/<video_id>")
def info(video_id):
    key = f"info:{video_id}"
    cached = _response_cache_get(key)
    if cached is not None:
        _record_view(video_id, cached)
        return jsonify(cached)

    with _track_processing(video_id, "info"):
        data = _extract_full(video_id)

        # 投稿者のアバター画像。yt-dlpの info dict には入っていないので、
        # watch pageのytInitialDataから channel/related と同じ要領で拾ってくる。
        channel_avatar_url = None
        want_base64 = request.args.get("base64", "1") != "0"
        try:
            page_resp = _fetch_page(_add_lang_params(_resolve_url(video_id)), timeout=10)
            if page_resp.status_code < 400:
                yt_data = _extract_yt_initial_data(page_resp.text)
                if yt_data:
                    channel_avatar_url = _find_video_owner_avatar(yt_data)
                    if not channel_avatar_url:
                        # videoOwnerRendererが見つからない場合の最終手段
                        channel_avatar_url = _find_named_image_url(yt_data, "avatar")
        except ApiError:
            pass

    result = _build_info_payload(data)
    result["channel_avatar"] = channel_avatar_url
    result["channel_avatar_base64"] = _image_to_data_uri(channel_avatar_url) if want_base64 else None
    _response_cache_set(key, "info", video_id, result)
    _record_view(video_id, result)

    result = dict(result)
    result["_cache"] = {
        "hit": False,
        "age_seconds": 0,
        "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS,
    }
    return jsonify(result)


# ---------- stream (全ストリームURL一覧。あればHLS直リンクも) ----------

_STREAM_FIELDS = [
    "format_id", "format_note", "ext", "resolution", "width", "height", "fps",
    "vcodec", "acodec", "abr", "vbr", "tbr", "asr", "audio_channels",
    "filesize", "filesize_approx", "protocol", "container", "dynamic_range",
    "language", "quality", "url",
]


def _build_stream_payload(video_id, data):
    formats = data.get("formats") or []
    streams = []
    hls_url = None
    for f in formats:
        entry = {k: f.get(k) for k in _STREAM_FIELDS}
        streams.append(entry)
        protocol = f.get("protocol") or ""
        if hls_url is None and "m3u8" in protocol:
            hls_url = f.get("url")

    if hls_url is None:
        hls_url = data.get("manifest_url")

    return _json_safe({
        "video_id": data.get("id") or video_id,
        "title": data.get("title"),
        "is_live": data.get("is_live", False),
        "streams": streams,
        "hls_url": hls_url,  # yt-dlpが返す実際のHLS(m3u8)直リンク。無い場合はnull
        # streams[].url はサーバーが解決した時のIPに紐付いていることがあり、
        # ブラウザから直接叩くと再生できない場合がある。その時はこちらを使う。
        # {format_id} を streams[].format_id に置き換えて叩けば、サーバー側で中継してくれる。
        "proxy_url_template": f"/api/proxy-stream/{video_id}?format_id={{format_id}}",
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })


def _resolve_direct_url(video_id, format_id, use_cache=True):
    """
    指定フォーマットのCDN直リンクを取得する。/api/proxy-stream 用に短時間キャッシュする
    (毎回のRangeリクエストごとにyt-dlpを叩き直すと遅すぎるため)。

    指定されたformat_idがその動画に存在しない場合(YouTube側がプログレッシブ
    フォーマットを提供していない動画がある)、エラーにせず"best"にフォールバックする。
    """
    cache_key = (video_id, format_id)
    if use_cache and cache_key in _STREAM_URL_CACHE:
        cached_url, expire_at = _STREAM_URL_CACHE[cache_key]
        if time.time() < expire_at:
            return cached_url

    source_url = _resolve_url(video_id)
    try:
        data = _extract(source_url, {"format": format_id})
    except ApiError:
        if format_id == "best":
            raise
        data = _extract(source_url, {"format": "best"})

    stream_url = data.get("url")
    if not stream_url and data.get("requested_formats"):
        stream_url = data["requested_formats"][0].get("url")
    if not stream_url:
        raise ApiError(404, "direct url not found for this format")

    _STREAM_URL_CACHE[cache_key] = (stream_url, time.time() + STREAM_URL_CACHE_TTL_SEC)
    return stream_url


@app.get("/api/proxy-stream/<video_id>")
def proxy_stream(video_id):
    """
    ブラウザから直接googlevideo等のCDN URLを叩くと、IPバインド(yt-dlpが解決したサーバーの
    IPからしかアクセスできない)の影響で再生できないことがある。このエンドポイントは
    サーバー側でストリームを取得してそのままクライアントへ中継することで、どの端末からでも
    確実に再生できるようにする。Rangeリクエストにも対応しているのでシークもできる。
    """
    format_id = request.args.get("format_id", "18")
    range_header = request.headers.get("Range")
    # googlevideo等のCDNは、素のPython requestsのUser-Agent(python-requests/x.x)だと
    # リクエストを弾いてくることがある。ブラウザっぽいヘッダを付けて回避する。
    fwd_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    }
    if range_header:
        fwd_headers["Range"] = range_header

    def _try_fetch(use_cache):
        url = _resolve_direct_url(video_id, format_id, use_cache=use_cache)
        return requests.get(url, headers=fwd_headers, stream=True, timeout=30)

    try:
        upstream = _try_fetch(use_cache=True)
        if upstream.status_code >= 400:
            upstream.close()
            raise requests.RequestException(f"upstream returned {upstream.status_code}")
    except requests.RequestException:
        # キャッシュしていたURLが失効している可能性があるので、1回だけ再解決してリトライ
        try:
            upstream = _try_fetch(use_cache=False)
        except requests.RequestException as e:
            raise ApiError(502, f"upstream fetch failed: {e}")
        if upstream.status_code >= 400:
            upstream.close()
            raise ApiError(502, f"upstream returned {upstream.status_code}")

    passthrough_headers = {}
    for h in ("Content-Range", "Content-Length", "Accept-Ranges", "Content-Type"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]
    passthrough_headers.setdefault("Accept-Ranges", "bytes")
    passthrough_headers.setdefault("Content-Type", "video/mp4")

    def gen():
        try:
            for chunk in upstream.iter_content(262144):
                if chunk:
                    yield chunk
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError):
            # ブラウザ側がシーク/画質切替/ページ離脱等で接続を切っただけの、よくあるケース。
            # サーバーログを例外で汚さないよう、ここで黙って終了する。
            pass
        finally:
            upstream.close()

    status_code = 206 if range_header and "Content-Range" in upstream.headers else upstream.status_code
    return Response(gen(), status=status_code, headers=passthrough_headers)


@app.get("/api/stream/<video_id>")
def stream(video_id):
    key = f"stream:{video_id}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    with _track_processing(video_id, "stream"):
        data = _extract_full(video_id)

    result = _build_stream_payload(video_id, data)
    _response_cache_set(key, "stream", video_id, result)

    result = dict(result)
    result["_cache"] = {
        "hit": False,
        "age_seconds": 0,
        "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS,
    }
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("YTDLP_API_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
