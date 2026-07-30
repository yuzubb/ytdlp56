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
import string
import secrets
import re
import html as html_module
import itsdangerous
import werkzeug.security
import json
import time
import base64
import gzip
import zlib
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


RESPONSE_CACHE_TTL_SECONDS = int(os.environ.get("YTDLP_API_CACHE_TTL_SECONDS", str(7 * 3600)))


def _get_or_create_secret(env_var_name, file_name, length=48):
    """
    環境変数で指定されていればそれを使う。無ければ、英数字のランダムな長い文字列を
    自動生成してファイルに保存し、次回起動時もその値を使い続ける(毎回変わると
    フロントエンド側の設定と食い違ってしまうため)。
    """
    from_env = os.environ.get(env_var_name, "")
    if from_env:
        return from_env

    secret_path = os.path.join(TMP_DIR_PATH, file_name)
    if os.path.isfile(secret_path):
        with open(secret_path, "r", encoding="utf-8") as f:
            saved = f.read().strip()
            if saved:
                return saved

    alphabet = string.ascii_letters + string.digits
    generated = "".join(secrets.choice(alphabet) for _ in range(length))
    with open(secret_path, "w", encoding="utf-8") as f:
        f.write(generated)
    print(f"[security] {env_var_name} が未設定だったため、ランダムな値を自動生成しました。")
    print(f"[security] 値は {secret_path} に保存済みです: {generated}")
    return generated


ADMIN_PASSWORD = _get_or_create_secret("YTDLP_API_ADMIN_PASSWORD", "admin_password.txt")


@app.before_request
def _log_api_access():
    """/api/ 配下へのアクセスをログに残すだけ(認証はしない)。
    キャッシュ削除等の破壊的操作は _require_admin_password() で別途保護している。"""
    path = request.path
    if not path.startswith("/api/"):
        return None
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    print(f"[access] {client_ip} -> {request.method} {path}")
    return None


def _require_admin_password():
    supplied = request.args.get("password", "")
    if supplied != ADMIN_PASSWORD:
        raise ApiError(403, "invalid password")



TRENDING_DATA_DIR = os.path.join(TMP_DIR_PATH, "trending_data")
os.makedirs(TRENDING_DATA_DIR, exist_ok=True)
VIEWS_JSON_PATH = os.path.join(TRENDING_DATA_DIR, "views.json")
_TRENDING_LOCK = threading.Lock()


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json_file(path, data):
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
        entry["channel_thumbnail"] = (
            data.get("channel_avatar_base64") or data.get("channel_avatar") or entry.get("channel_thumbnail")
        )
        entry["thumbnail"] = data.get("thumbnail") or entry.get("thumbnail")
        entry["duration"] = data.get("duration") or entry.get("duration")
        entry["last_viewed"] = time.time()
        views[video_id] = entry
        _save_json_file(VIEWS_JSON_PATH, views)


AUTH_DATA_DIR = os.path.join(TMP_DIR_PATH, "auth_data")
os.makedirs(AUTH_DATA_DIR, exist_ok=True)
USERS_JSON_PATH = os.path.join(AUTH_DATA_DIR, "users.json")
_AUTH_LOCK = threading.Lock()

SESSION_MAX_AGE = 7 * 24 * 3600

_SESSION_SECRET_KEY = _get_or_create_secret("YTDLP_API_SESSION_SECRET", "session_secret.txt")
_session_serializer = itsdangerous.URLSafeTimedSerializer(_SESSION_SECRET_KEY, salt="yuzutube-session")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _load_users():
    with _AUTH_LOCK:
        return _load_json_file(USERS_JSON_PATH)


def _save_users(users):
    with _AUTH_LOCK:
        _save_json_file(USERS_JSON_PATH, users)


def _create_session_token(email):
    return _session_serializer.dumps({"email": email})


def _verify_session_token(token):
    """トークンが正しく、かつ1週間以内に発行されたものであればemailを返す。
    改ざんされていたり期限切れなら例外を投げずにNoneを返すだけにしておく。"""
    if not token:
        return None
    try:
        data = _session_serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (itsdangerous.BadSignature, itsdangerous.SignatureExpired):
        return None
    return data.get("email")


@app.post("/api/auth/signup")
def auth_signup():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    client_ip = body.get("ip") or request.headers.get("X-Forwarded-For", request.remote_addr or "")

    if not _EMAIL_RE.match(email):
        raise ApiError(400, "メールアドレスの形式が正しくありません")
    if len(password) < 8:
        raise ApiError(400, "パスワードは8文字以上にしてください")

    users = _load_users()
    if email in users:
        raise ApiError(409, "このメールアドレスは既に登録されています")

    users[email] = {
        "password_hash": werkzeug.security.generate_password_hash(password),
        "created_at": time.time(),
        "signup_ip": client_ip,
        "last_login_ip": client_ip,
        "last_login_at": time.time(),
    }
    _save_users(users)
    print(f"[auth] signup: {email} from {client_ip}")

    token = _create_session_token(email)
    return jsonify({"email": email, "token": token, "max_age": SESSION_MAX_AGE})


@app.post("/api/auth/login")
def auth_login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    client_ip = body.get("ip") or request.headers.get("X-Forwarded-For", request.remote_addr or "")

    users = _load_users()
    user = users.get(email)
    if not user or not werkzeug.security.check_password_hash(user["password_hash"], password):
        print(f"[auth] login failed: {email} from {client_ip}")
        raise ApiError(401, "メールアドレスまたはパスワードが違います")

    user["last_login_ip"] = client_ip
    user["last_login_at"] = time.time()
    _save_users(users)
    print(f"[auth] login ok: {email} from {client_ip}")

    token = _create_session_token(email)
    return jsonify({"email": email, "token": token, "max_age": SESSION_MAX_AGE})


@app.post("/api/auth/verify")
def auth_verify():
    body = request.get_json(silent=True) or {}
    email = _verify_session_token(body.get("token"))
    if not email:
        raise ApiError(401, "セッションが無効か期限切れです")
    return jsonify({"email": email})


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
            "channel_thumbnail": entry.get("channel_thumbnail"),
            "thumbnail": entry.get("thumbnail"),
            "duration": entry.get("duration"),
            "view_count_text": f"このサイトで{entry.get('view_count', 0)}回視聴",
            "length_text": None,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return entries


_STREAM_URL_CACHE = {}
STREAM_URL_CACHE_TTL_SEC = int(os.environ.get("YTDLP_API_STREAM_URLCACHE_TTL", "1800"))


SERVER_ID = os.environ.get("YTDLP_API_SERVER_ID", "1")
SERVER_NAME = os.environ.get("YTDLP_API_SERVER_NAME", f"Server {SERVER_ID}")
SERVER_ROLE = os.environ.get("YTDLP_API_ROLE", "primary")
START_TIME = time.time()

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
        threshold = now - RESPONSE_CACHE_TTL_SECONDS
        conn.execute("DELETE FROM response_cache WHERE created_at < ?", (threshold,))



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


def _resolve_channel_url(channel_id, tab="videos"):
    """
    channel_idがURLならデコードしてそのまま使う。
    '@handle' 形式、'UCxxxx' 形式のチャンネルID、素のハンドル名のどれでも受け付ける。
    tabは "videos"(通常の投稿動画) / "streams"(過去のライブ配信アーカイブ) /
    "shorts" / "playlists" のいずれか。YouTubeチャンネルページのタブ切り替えに対応する。
    """
    decoded = urllib.parse.unquote(channel_id)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", decoded):
        return decoded
    if decoded.startswith("@"):
        return f"https://www.youtube.com/{decoded}/{tab}"
    if decoded.startswith("UC") and len(decoded) > 10:
        return f"https://www.youtube.com/channel/{decoded}/{tab}"
    return f"https://www.youtube.com/@{decoded}/{tab}"


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


_CHROME_VERSION = "126.0.6478.127"
_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROME_VERSION.split('.')[0]}.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    # 本物のChromeが送ってくるClient Hints/Sec-Fetch系ヘッダー。
    # これが無いとBotスコアリングで不利になることがあるため一通り揃えている。
    "Sec-Ch-Ua": f'"Not/A)Brand";v="8", "Chromium";v="{_CHROME_VERSION.split(".")[0]}", "Google Chrome";v="{_CHROME_VERSION.split(".")[0]}"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


def _decompress_body(raw, content_encoding):
    """Accept-Encodingでgzip/deflateを許可した都合上、レスポンスが圧縮されて
    返ってくることがある。urllibはrequestsと違って自動展開してくれないので、
    Content-Encodingを見て自前で展開する。"""
    encoding = (content_encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            return zlib.decompress(raw)
    except (OSError, zlib.error):
        return raw
    return raw


def _fetch_page(url, timeout=60):
    """
    YouTubeの生HTMLページを取ってくる。requestsではなくPython標準ライブラリ(urllib)を
    使っている。requests(urllib3)だとブロックされるケースでも、urllibだとTLS/HTTPの
    フィンガープリントが変わって通ることがあるため、生スクレイピング系はこちらに統一した。
    cookies.txtが置いてあれば、そのcookieも一緒に送る(consent回避用のCONSENT/SOCSは常に付与)。
    """
    headers = dict(_PAGE_HEADERS)
    headers["Cookie"] = _cookie_header_string()
    req = urllib.request.Request(url, headers=headers)
    content_encoding = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
            content_encoding = resp.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if e.fp else b""
        content_encoding = e.headers.get("Content-Encoding", "") if e.headers else ""
    except urllib.error.URLError as e:
        raise ApiError(502, f"failed to fetch page: {e.reason}")
    raw = _decompress_body(raw, content_encoding)
    return _PageResponse(status, raw.decode("utf-8", errors="replace"))


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"lang": ["ja"]}},
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
        # yt-dlp自体のリクエストにも、本物のChromeに近いヘッダーを持たせておく
        # (yt-dlpは内部でgzip/br展開を自前で処理してくれるので、ここはAccept-Encodingを
        # 含めても安全)。
        "http_headers": {
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": _PAGE_HEADERS["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    }
    if os.path.isfile(COOKIES_FILE_PATH):
        opts["cookiefile"] = COOKIES_FILE_PATH
    if extra:
        extra = dict(extra)
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
        "http_headers": {
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": _PAGE_HEADERS["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
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




@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "uptime_seconds": _uptime_seconds()})



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
    _STREAM_URL_CACHE.clear()
    return jsonify({
        "deleted": "all",
        "index_entries_removed": index_count,
        "response_cache_entries_removed": response_count,
    })



@app.get("/api/search")
def search():
    """
    ?q=検索語 でYouTube検索。?continuation=続きのトークン で次のページを取得できる
    (無限スクロール用)。レスポンスの next_continuation を次回のリクエストに渡せばよい。

    まずYouTubeの検索結果ページを直接スクレイピングする(related/trendingと同じ
    「動画カードを総当たりで拾う」方式)。こちらだと各結果にチャンネルの小さい
    アイコン画像(channel_thumbnail)も付いてくる。yt-dlpのflat検索(ytsearchN:)には
    このアイコン情報が無いため、フォールバック用にとどめている
    (ただしyt-dlpフォールバック時は続きのページ取得はできない)。
    """
    q = request.args.get("q")
    if not q:
        raise ApiError(400, "query parameter 'q' is required")
    limit = max(1, min(int(request.args.get("limit", 20)), 50))
    continuation_token = request.args.get("continuation")

    def _build_search_url():
        return "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)

    if continuation_token:
        with _track_processing(f"search:{q}", "search"):
            resp = _fetch_page(_add_lang_params(_build_search_url()), timeout=60)
            api_key, context = _extract_ytcfg(resp.text)
            if not context:
                # ytcfgから取れなくても、最低限のクライアント情報だけで継続取得を試みる
                context = {"client": {"hl": "ja", "gl": "JP", "clientName": "WEB", "clientVersion": "2.20240101.00.00"}}
            cont_data = _fetch_youtube_continuation("search", api_key, context, continuation_token)
            entries = _parse_video_cards(cont_data, None, limit)
            next_token = _find_continuation_token(cont_data)

        return jsonify(_json_safe({
            "query": q,
            "result_count": len(entries),
            "entries": entries,
            "next_continuation": next_token,
        }))

    key = f"search:{q}:{limit}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    entries = None
    next_token = None
    with _track_processing(f"search:{q}", "search"):
        try:
            resp = _fetch_page(_add_lang_params(_build_search_url()), timeout=60)
            if resp.status_code < 400:
                yt_data = _extract_yt_initial_data(resp.text)
                if yt_data:
                    entries = _parse_video_cards(yt_data, None, limit)
                    next_token = _find_continuation_token(yt_data)
        except ApiError:
            pass

        if not entries:
            info = _extract_flat(f"ytsearch{limit}:{q}")
            entries = [_slim_entry(e) for e in (info.get("entries") or [])]
            next_token = None

    result = _json_safe({
        "query": q,
        "result_count": len(entries),
        "entries": entries,
        "next_continuation": next_token,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "search", q, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/playlist/<playlist_id>")
def playlist(playlist_id):
    """プレイリストのメタ情報+収録動画一覧。?limit=&offset=で範囲指定。
    まずプレイリストページを直接スクレイピングする(search/relatedと同じ方式)。
    こちらだと動画ごとに投稿チャンネルの小さいアイコン画像(channel_thumbnail)も
    付いてくる(プレイリストの収録動画は投稿者がバラバラなことがあるため、
    チャンネルページの時のように1つのアバターを使い回すことができない)。
    失敗した場合のみyt-dlpのflat抽出にフォールバックする。
    """
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    offset = max(0, int(request.args.get("offset", 0)))

    key = f"playlist:{playlist_id}:{limit}:{offset}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    url = _resolve_playlist_url(playlist_id)
    entries = None
    title = None
    uploader = None
    channel_id = None
    with _track_processing(playlist_id, "playlist"):
        try:
            resp = _fetch_page(_add_lang_params(url), timeout=60)
            if resp.status_code < 400:
                yt_data = _extract_yt_initial_data(resp.text)
                if yt_data:
                    all_cards = _parse_video_cards(yt_data, None, offset + limit)
                    entries = all_cards[offset:offset + limit]
                    title = _extract_og_title(resp.text)
        except ApiError:
            pass

        info = None
        if not entries:
            info = _extract_flat(url, playliststart=offset + 1, playlistend=offset + limit)
            entries = [_slim_entry(e) for e in (info.get("entries") or [])]
            title = info.get("title") or title
            uploader = info.get("uploader") or info.get("channel")
            channel_id = info.get("channel_id") or info.get("uploader_id")

    result = _json_safe({
        "playlist_id": (info.get("id") if info else None) or playlist_id,
        "title": title,
        "uploader": uploader,
        "channel_id": channel_id,
        "webpage_url": url,
        "description": info.get("description") if info else None,
        "entry_count_total": (info.get("playlist_count") if info else None),
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
    チャンネルのメタ情報+投稿一覧(flat抽出)。?limit=&offset=で範囲指定。
    channel_id は '@handle'、'UCxxxx'、素のハンドル名、フルURLのいずれでも指定可能。
    ?tab= で YouTube本家のタブ切り替えに相当する一覧を取得できる:
      videos(既定・通常の投稿動画) / streams(過去のライブ配信アーカイブ) /
      shorts / playlists(再生リスト一覧)

    アバター/バナーはyt-dlpのextract_flatだけでは取れない(特にバナー)ため、
    チャンネルページのytInitialDataを別途取得してrelated/trendingと同じ要領で
    "avatar" / "banner" というキーを持つノードを総当たりで探している。
    見つかった画像はサーバー側で取得してbase64のdata URIにしてから返す
    (ホットリンク周りの問題を避けるため。?base64=0を付けると元のURLのままにできる)。

    entries内の各動画にも、このチャンネル自身のアバターをchannel_thumbnailとして
    埋め込んでいる(このページの動画は全部同じチャンネルのものなので、動画1本ずつに
    ついて別途アバターを探しに行く必要が無いため)。
    """
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    offset = max(0, int(request.args.get("offset", 0)))
    want_base64 = request.args.get("base64", "1") != "0"
    tab = request.args.get("tab", "videos")
    if tab not in ("videos", "streams", "shorts", "playlists"):
        tab = "videos"

    key = f"channel:{channel_id}:{tab}:{limit}:{offset}:{want_base64}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    url = _resolve_channel_url(channel_id, tab)
    with _track_processing(channel_id, "channel"):
        info = _extract_flat(url, playliststart=offset + 1, playlistend=offset + limit)

        avatar_url = None
        banner_url = None
        available_tabs = None
        page_html = None
        try:
            page_resp = _fetch_page(_add_lang_params(url), timeout=10)
            if page_resp.status_code < 400:
                page_html = page_resp.text
        except ApiError:
            pass

        if page_html:
            avatar_url = _extract_og_image(page_html)
            yt_data = _extract_yt_initial_data(page_html)
            if yt_data:
                if not avatar_url:
                    avatar_url = _find_channel_metadata_avatar(yt_data)
                if not avatar_url:
                    avatar_url = _find_named_image_url(yt_data, "avatar")
                banner_url = _find_named_image_url(yt_data, "banner")
                available_tabs = _find_available_channel_tabs(yt_data) or None

        if not avatar_url:
            thumbs = info.get("thumbnails") or []
            if thumbs:
                avatar_url = thumbs[-1].get("url")

        avatar_b64 = _image_to_data_uri(avatar_url) if want_base64 else None
        banner_b64 = _image_to_data_uri(banner_url) if want_base64 else None

    channel_name = info.get("channel") or info.get("uploader") or info.get("title")
    avatar_for_entries = avatar_b64 or avatar_url

    entries = []
    for e in (info.get("entries") or []):
        slim = _slim_entry(e)
        slim["channel"] = slim.get("channel") or channel_name
        slim["channel_id"] = slim.get("channel_id") or (info.get("channel_id") or channel_id)
        slim["channel_thumbnail"] = avatar_for_entries
        entries.append(slim)

    result = _json_safe({
        "channel_id": info.get("channel_id") or info.get("id") or channel_id,
        "channel": channel_name,
        "channel_follower_count": info.get("channel_follower_count"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "avatar": avatar_url,
        "avatar_base64": avatar_b64,
        # そのチャンネルに実際に存在するタブだけを返す(空のタブをUIに出さないため)。
        # 取得できなかった場合はNoneにして、フロント側は「全部あるものとして表示」に
        # フォールバックできるようにしておく。
        "available_tabs": available_tabs,
        "banner": banner_url,
        "banner_base64": banner_b64,
        "tab": tab,
        "entry_count_total": info.get("playlist_count"),
        "entry_count_returned": len(entries),
        "entries": entries,
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })
    _response_cache_set(key, "channel", channel_id, result)

    result = dict(result)
    result["_cache"] = {"hit": False, "age_seconds": 0, "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS}
    return jsonify(result)


@app.get("/api/subtitles/<video_id>")
def subtitles(video_id):
    """
    指定言語の字幕をWebVTT形式で返す。?auto=1で自動生成字幕、既定(0)は手動字幕。
    利用可能な言語コードの一覧は /api/info の subtitles_languages /
    automatic_captions_languages で確認できる。
    """
    lang = request.args.get("lang", "ja")
    auto = request.args.get("auto", "0") == "1"

    with _track_processing(video_id, "subtitles"):
        data = _extract_full(video_id)

    pool = data.get("automatic_captions" if auto else "subtitles") or {}
    tracks = pool.get(lang)
    if not tracks:
        kind = "自動生成" if auto else "手動"
        raise ApiError(404, f"{kind}字幕が見つかりません (lang={lang})")

    track = next((t for t in tracks if t.get("ext") == "vtt"), tracks[0])
    sub_url = track.get("url")
    if not sub_url:
        raise ApiError(404, "subtitle url not found")

    resp = _fetch_page(sub_url, timeout=60)
    if resp.status_code >= 400:
        raise ApiError(502, f"failed to fetch subtitle: HTTP {resp.status_code}")

    return Response(resp.text, mimetype="text/vtt")


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

        resp = _fetch_page(chat_url, timeout=60)
        if resp.status_code >= 400:
            raise ApiError(502, f"failed to fetch live chat data: HTTP {resp.status_code}")

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


# YouTubeの一般公開Webクライアントが常に使っている既知の固定キー(個人のものではなく、
# YouTube全体で共通の値)。ytcfgからの抽出に失敗した時のフォールバックとして使う。
_FALLBACK_INNERTUBE_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"


def _extract_ytcfg(html):
    """
    ページ内には ytcfg.set({...}) が複数回出てくることがあり、1回目の呼び出しだけでは
    INNERTUBE_API_KEY や INNERTUBE_CONTEXT が揃っていないことがある。
    見つかった全部をマージしてから必要な値を取り出す。
    """
    merged = {}
    for m in re.finditer(r"ytcfg\.set\(\s*(\{.+?\})\s*\)\s*;", html):
        try:
            cfg = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(cfg, dict):
            merged.update(cfg)
    if not merged:
        return _FALLBACK_INNERTUBE_API_KEY, None
    api_key = merged.get("INNERTUBE_API_KEY") or _FALLBACK_INNERTUBE_API_KEY
    return api_key, merged.get("INNERTUBE_CONTEXT")


def _find_continuation_token(yt_data):
    for node in _walk(yt_data):
        if "continuationItemRenderer" in node:
            token = _dig(node, "continuationItemRenderer", "continuationEndpoint", "continuationCommand", "token")
            if token:
                return token
    return None


def _fetch_youtube_continuation(endpoint, api_key, context, continuation_token, timeout=60):
    """
    検索結果・チャンネル動画一覧の「続き」を取得する。YouTubeの内部API
    (youtubei)を直接叩く。endpointは "search" か "browse"。
    """
    url = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={api_key}"
    body = json.dumps({"context": context, "continuation": continuation_token}).encode("utf-8")
    headers = dict(_PAGE_HEADERS)
    headers["Content-Type"] = "application/json"
    headers["Cookie"] = _cookie_header_string()
    # youtubei側はクライアント識別ヘッダーも見ていることがあるため、contextから拾って付与する
    client = (context or {}).get("client", {})
    if client.get("clientName"):
        headers["X-Youtube-Client-Name"] = "1"
    if client.get("clientVersion"):
        headers["X-Youtube-Client-Version"] = client["clientVersion"]

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    content_encoding = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            content_encoding = resp.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        content_encoding = e.headers.get("Content-Encoding", "") if e.headers else ""
    except urllib.error.URLError as e:
        raise ApiError(502, f"failed to fetch continuation: {e.reason}")

    raw = _decompress_body(raw, content_encoding)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ApiError(502, f"failed to parse continuation response: {e}")


def _extract_yt_initial_data(html):
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
    if not node:
        return None
    if "simpleText" in node:
        return node["simpleText"]
    runs = node.get("runs") or []
    text = "".join(r.get("text", "") for r in runs)
    return text or None


def _dig(obj, *path):
    cur = obj
    for key in path:
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _find_thumbnails_under(node):
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


_TAB_URL_TO_KEY = {
    "videos": "videos",
    "streams": "streams",
    "shorts": "shorts",
    "playlists": "playlists",
}


def _find_available_channel_tabs(yt_data):
    """
    チャンネルページのytInitialDataから、実際に存在するタブ(動画/ショート/ライブ/
    再生リスト等)だけを拾う。YouTube側はそのチャンネルが使っていないタブ
    (ショート投稿が無い等)をそもそもtabRendererに含めてこないので、
    これを見れば「空だから隠すべきタブ」が分かる。
    """
    found = []
    for node in _walk(yt_data):
        if "tabRenderer" not in node:
            continue
        tab = node["tabRenderer"]
        url = _dig(tab, "endpoint", "commandMetadata", "webCommandMetadata", "url") or ""
        segment = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        key = _TAB_URL_TO_KEY.get(segment)
        if key and key not in found:
            found.append(key)
    return found


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


_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_OG_TITLE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', re.IGNORECASE
)


def _extract_og_title(html):
    m = _OG_TITLE_RE.search(html) or _OG_TITLE_RE_ALT.search(html)
    if not m:
        return None
    return html_module.unescape(m.group(1))


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


# YouTube検索の ?sp= フィルター値。protobufをbase64エンコードした値で、
# 複数条件を自由に組み合わせるには本来protobufのマージが必要になるため、
# ここでは動作確認が取れている「単体で使える値」だけをホワイトリストにしている。
_CHANNEL_ID_PATHS = (
    ("channelThumbnailSupportedRenderers", "channelThumbnailWithLinkRenderer",
     "navigationEndpoint", "browseEndpoint", "browseId"),
    ("shortBylineText", "runs", 0, "navigationEndpoint", "browseEndpoint", "browseId"),
    ("longBylineText", "runs", 0, "navigationEndpoint", "browseEndpoint", "browseId"),
)

_CHANNEL_THUMBNAIL_PATHS = (
    ("channelThumbnailSupportedRenderers", "channelThumbnailWithLinkRenderer", "thumbnail", "thumbnails"),
    ("channelThumbnail", "thumbnails"),
)


def _parse_lockup_view_model(node):
    """
    YouTubeの新しいUI形式(lockupViewModel)を解析する。
    videoRenderer等の従来形式とは構造がまるで違う(videoIdはcontentId、
    titleはmetadata.lockupMetadataViewModel.title.content、という具合に
    ネストが深く名前も違う)ため、_looks_like_videoの単純な総当たりでは拾えない。
    このノード形式専用の解析経路を別途用意している。
    """
    lockup = node.get("lockupViewModel")
    if not isinstance(lockup, dict):
        return None

    video_id = lockup.get("contentId")
    if not video_id:
        return None

    metadata_vm = _dig(lockup, "metadata", "lockupMetadataViewModel") or {}
    title = _dig(metadata_vm, "title", "content")
    if not title:
        return None

    content_metadata = _dig(metadata_vm, "metadata", "contentMetadataViewModel") or {}
    metadata_rows = content_metadata.get("metadataRows") or []

    channel_row = _dig(metadata_rows, 0, "metadataParts", 0) if metadata_rows else None
    channel_name = _dig(channel_row, "text", "content") if channel_row else None

    stats_row = _dig(metadata_rows, 1, "metadataParts") if len(metadata_rows) > 1 else None
    views_text = _dig(stats_row, 0, "text", "content") if stats_row else None

    thumbnail_sources = (
        _dig(lockup, "contentImage", "thumbnailViewModel", "image", "sources")
        or _dig(lockup, "contentImage", "image", "sources")
        or []
    )
    thumbnail_url = thumbnail_sources[-1]["url"] if thumbnail_sources else None

    avatar_data = _dig(metadata_vm, "image", "decoratedAvatarViewModel", "avatar", "avatarViewModel")
    channel_avatar = _dig(avatar_data, "image", "sources", 0, "url") if avatar_data else None
    channel_url = _dig(
        metadata_vm, "image", "decoratedAvatarViewModel", "rendererContext",
        "commandContext", "onTap", "innertubeCommand", "browseEndpoint", "canonicalBaseUrl",
    )
    channel_id = None
    if channel_url:
        channel_id = channel_url.strip("/").rsplit("/", 1)[-1] or None

    overlays = (
        _dig(lockup, "contentImage", "thumbnailViewModel", "overlays")
        or _dig(lockup, "contentImage", "overlays")
        or []
    )
    length_text = None
    for overlay in overlays:
        badge_vm = _dig(overlay, "thumbnailOverlayBadgeViewModel", "thumbnailBadges", 0, "thumbnailBadgeViewModel")
        if badge_vm and badge_vm.get("text"):
            length_text = badge_vm["text"]
            break

    return {
        "video_id": video_id,
        "title": title,
        "channel": channel_name,
        "channel_id": channel_id,
        "channel_thumbnail": channel_avatar,
        "length_text": length_text,
        "view_count_text": views_text,
        "thumbnail": thumbnail_url,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def _looks_like_video(node):
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

    従来形式(videoRenderer等)と新形式(lockupViewModel)の両方に対応している。
    """
    seen = set()
    entries = []

    for node in _walk(yt_data):
        if not isinstance(node, dict):
            continue

        if "lockupViewModel" in node:
            parsed = _parse_lockup_view_model(node)
            if not parsed or parsed["video_id"] == exclude_id or parsed["video_id"] in seen:
                continue
            seen.add(parsed["video_id"])
            entries.append(parsed)
            if len(entries) >= limit:
                break
            continue

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


_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
})
_http.cookies.set("CONSENT", "YES+1", domain=".youtube.com")
_http.cookies.set("SOCS", "CAI", domain=".youtube.com")

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
        resp = _fetch_page(_add_lang_params(watch_url), timeout=60)
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
    sanitized["resolution_reference"] = ref
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

        channel_avatar_url = None
        want_base64 = request.args.get("base64", "1") != "0"
        try:
            page_resp = _fetch_page(_add_lang_params(_resolve_url(video_id)), timeout=10)
            if page_resp.status_code < 400:
                yt_data = _extract_yt_initial_data(page_resp.text)
                if yt_data:
                    channel_avatar_url = _find_video_owner_avatar(yt_data)
                    if not channel_avatar_url:
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
        "hls_url": hls_url,
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

    if request.args.get("download", "0") == "1":
        ext = "mp4"
        content_type = passthrough_headers.get("Content-Type", "")
        if "webm" in content_type:
            ext = "webm"
        elif "audio" in content_type:
            ext = "m4a"
        passthrough_headers["Content-Disposition"] = f'attachment; filename="{video_id}_{format_id}.{ext}"'

    def gen():
        try:
            for chunk in upstream.iter_content(262144):
                if chunk:
                    yield chunk
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError):
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
