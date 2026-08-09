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
import hashlib
import re
import html as html_module
import itsdangerous
import werkzeug.security
import json
import time
import sys
import base64
import binascii
import gzip
import zlib
import sqlite3
import threading
import concurrent.futures
import urllib.parse
import urllib.request
import urllib.error
import contextlib
from datetime import datetime

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response, render_template, abort

# server.pyと同じディレクトリの .env を読み込む(無ければ何もしない)。
# 既にシェルでexportされている環境変数の方を優先する(override=Falseが既定)。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = Flask(__name__)

_LOG_COLORS = {
    "gray": "\033[90m", "cyan": "\033[36m", "green": "\033[32m",
    "yellow": "\033[33m", "red": "\033[31m", "magenta": "\033[35m",
    "blue": "\033[34m", "bold": "\033[1m", "reset": "\033[0m",
}
_LOG_USE_COLOR = sys.stdout.isatty()


def _c(text, color):
    if not _LOG_USE_COLOR:
        return text
    return f"{_LOG_COLORS.get(color, '')}{text}{_LOG_COLORS['reset']}"


def log(tag, message, color="cyan"):
    timestamp = _c(datetime.now().strftime("%H:%M:%S"), "gray")
    print(f"{timestamp} {_c(f'[{tag.upper()}]', color)} {message}")


TMP_DIR_PATH = os.environ.get("YTDLP_API_TMP", os.path.join(os.path.expanduser("~"), "ytdlp_api_tmp"))

# VPNだと接続断や自動再接続の制御が難しいため、代わりにHTTP/SOCSプロキシを直接指定できる
# ようにしている。例: "http://user:pass@host:port" や "socks5://host:port"。
# 未設定なら何もせず、これまで通り直接通信する。
PROXY_URL = os.environ.get("YTDLP_API_PROXY_URL", "").strip()
os.makedirs(TMP_DIR_PATH, exist_ok=True)

COOKIES_FILE_PATH = os.environ.get(
    "YTDLP_API_COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
)


def _discover_cookie_files():
    """
    複数のcookies.txtを順番に試せるようにする。
    - 環境変数 YTDLP_API_COOKIES_FILE にカンマ区切りで複数パスを書けばそれを使う。
    - 未指定なら、server.pyと同じディレクトリの cookies.txt, cookies2.txt, cookies3.txt...
      (存在するものだけ)を連番で自動的に拾う。
    1つのアカウントのcookieがダメ(bot判定/フォーマット取得失敗)だった時に、
    次のアカウントのcookieへ自動的に切り替えるための一覧。
    """
    env_value = os.environ.get("YTDLP_API_COOKIES_FILE", "")
    if "," in env_value:
        paths = [p.strip() for p in env_value.split(",") if p.strip()]
        return [p for p in paths if os.path.isfile(p)]

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [COOKIES_FILE_PATH]
    n = 2
    while True:
        candidate = os.path.join(base_dir, f"cookies{n}.txt")
        if not os.path.isfile(candidate):
            break
        candidates.append(candidate)
        n += 1
    # 重複除去(COOKIES_FILE_PATHが手動でcookies2.txt等を指していた場合のケア)しつつ順序維持
    seen = set()
    result = []
    for p in candidates:
        if os.path.isfile(p) and p not in seen:
            seen.add(p)
            result.append(p)
    return result


COOKIES_FILE_PATHS = _discover_cookie_files()


class _SimpleCookie:
    """http.cookiejar.Cookieの代わりに使う最小限の入れ物(.name/.value/.domainだけ)。"""
    __slots__ = ("name", "value", "domain")

    def __init__(self, name, value, domain):
        self.name = name
        self.value = value
        self.domain = domain


def _load_cookiejar():
    """
    cookies.txt (Netscape形式)を読み込む。無い場合はNoneを返すだけで、
    呼び出し側はcookie無しの状態にフォールバックできる。

    Python標準の http.cookiejar.MozillaCookieJar は、ファイル内のたった1行でも
    形式がおかしいと(タブ区切りで7項目無い等)ファイル全体の読み込みごと
    失敗してしまう(せっかく他の行が正しくても全部無視されてしまう)。
    それだと実害が大きいので、ここでは1行ずつ自前でパースして、
    おかしい行だけ読み飛ばす方式にしている。
    """
    if not os.path.isfile(COOKIES_FILE_PATH):
        return None
    cookies = []
    try:
        with open(COOKIES_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                domain, _flag, _path, _secure, _expires, name, value = parts
                if not name:
                    continue
                cookies.append(_SimpleCookie(name, value, domain))
    except OSError:
        return None
    return cookies or None


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
    log("security", f"{env_var_name} が未設定だったため、ランダムな値を自動生成しました", "yellow")
    log("security", f"値は {secret_path} に保存済みです: {generated}", "yellow")
    return generated


ADMIN_PASSWORD = _get_or_create_secret("YTDLP_API_ADMIN_PASSWORD", "admin_password.txt")


_TOKEN_EXEMPT_API_PATHS = {"/api/token/issue", "/api/token/verify"}


def _get_client_ip():
    """
    X-Forwarded-Forは複数ホップを経由するとカンマ区切りで連なることがあるため
    (例: "実際の訪問者IP,フロントエンドサーバーのIP")、一番左(＝一番オリジナルに近い)
    を採用する。
    """
    raw = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    return raw.split(",")[0].strip()


_BAN_EXEMPT_API_PATHS = {
    "/api/inquiries", "/api/auth/verify", "/api/auth/login", "/api/auth/signup",
    "/api/health", "/api/frontend-version", "/api/announcement", "/api/ban/check",
}


@app.before_request
def _enforce_ip_ban():
    """
    BANされたIP、またはBANされたメールアドレスでログイン中のアクセスは、
    お問い合わせ関連・ログイン関連の最低限のAPIしか通さない
    (検索・動画視聴・その他の機能は一切使えなくする)。
    お問い合わせ自体の個別GET/POST(/api/inquiries/<id>等)はprefixで判定する。
    """
    path = request.path
    if not (path == "/api" or path.startswith("/api/")):
        return None
    if path in _BAN_EXEMPT_API_PATHS or path.startswith("/api/inquiries"):
        return None

    client_ip = _get_client_ip()
    ip_ban = _is_ip_banned(client_ip)

    token = request.headers.get("X-Session-Token", "")
    email = _verify_session_token(token) if token else None
    email_ban = _is_email_banned(email) if email else None

    if ip_ban or email_ban:
        log("denied", f"BAN済みアクセスを拒否: ip={client_ip} email={email} -> {path}", "red")
        raise ApiError(403, "利用を制限されています。お問い合わせフォームからご連絡ください。")
    return None


@app.before_request
def _enforce_api_token():
    """
    /api (ドキュメントページ) と /api/ 配下は、有効なトークンが無いと404を返す。
    401/403ではなくあえて404にしているのは、「何かあるけど弾かれている」ことすら
    悟らせないため(存在自体が分からないようにする、という要望に対応)。
    トークンの発行・検証エンドポイント自体は、そもそもトークンを取得する手段なので例外。

    ytdlp_frontendだけは、公開トークンではなく専用の合言葉(FRONTEND_BYPASS_SECRET)を
    X-Frontend-Secretヘッダで送ることでこのチェックを素通りできる。一般の人はこの値を
    知りようがないので安全性は変わらない。
    """
    path = request.path
    if path != "/api" and not path.startswith("/api/"):
        return None

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")

    if path in _TOKEN_EXEMPT_API_PATHS:
        log("access", f"{client_ip} -> {request.method} {path}", "green")
        return None

    if request.headers.get("X-Frontend-Secret", "") == FRONTEND_BYPASS_SECRET:
        log("access", f"{client_ip} -> {request.method} {path} (frontend)", "blue")
        return None

    token = request.headers.get("X-API-Token") or request.args.get("token", "")
    result = _verify_public_token(token) if token else {"valid": False}
    if not result.get("valid"):
        log("denied", f"{client_ip} -> {request.method} {path} (no/invalid token)", "red")
        abort(404)

    log("access", f"{client_ip} -> {request.method} {path}", "green")
    return None


def _require_admin_password():
    supplied = request.args.get("password", "")
    if supplied != ADMIN_PASSWORD:
        raise ApiError(403, "invalid password")



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


AUTH_DATA_DIR = os.path.join(TMP_DIR_PATH, "auth_data")
os.makedirs(AUTH_DATA_DIR, exist_ok=True)
USERS_JSON_PATH = os.path.join(AUTH_DATA_DIR, "users.json")
_AUTH_LOCK = threading.RLock()  # update_my_profile等、ロック取得中に_load_users/_save_usersを
                                  # 呼ぶ箇所があり、それらの内部でも同じロックを取るため再入可能にしている

SESSION_MAX_AGE = 7 * 24 * 3600

_SESSION_SECRET_KEY = _get_or_create_secret("YTDLP_API_SESSION_SECRET", "session_secret.txt")
_session_serializer = itsdangerous.URLSafeTimedSerializer(_SESSION_SECRET_KEY, salt="yuzutube-session")

# ytdlp_frontendだけが知っている固定の合言葉。これを持っていれば、一般公開している
# 1日有効のトークンを毎回取りに行かなくても /api/* を素通りできる(一般の人はこの値を
# 知りようがないので、公開トークン方式のセキュリティには影響しない)。
FRONTEND_BYPASS_SECRET = _get_or_create_secret("YTDLP_API_FRONTEND_SECRET", "frontend_secret.txt")

# お問い合わせ対応ができる「オーナー(管理者)」のメールアドレス。
# カンマ区切りで複数指定できる。例: YTDLP_API_OWNER_EMAILS=a@example.com,b@example.com
OWNER_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("YTDLP_API_OWNER_EMAILS", "").split(",")
    if e.strip()
}

# 動画タイトルのAI判定に使う(任意機能)。未設定なら従来のNGワード判定だけで動く。
# 無料枠があるモデルを既定にしているが、Groq側のモデル名・無料枠は変更が
# 頻繁なので、古くなっていたら環境変数で上書きしてください。
GROQ_API_KEY = os.environ.get("YTDLP_API_GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("YTDLP_API_GROQ_MODEL", "llama-3.3-70b-versatile").strip()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------- プロフィール(表示名・ユーザーID・アイコン)のバリデーション ----------
# 表示名・ユーザーIDはHTMLタグ的な文字を弾いておく(XSS対策の多層防御。
# フロントエンド側でも表示時に必ずエスケープしているが、サーバー側でも
# 危険な文字自体を保存させない)。
_DISPLAY_NAME_MAX_LEN = 30
_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
_DANGEROUS_CHARS_RE = re.compile(r'[<>"\'&]')
_AVATAR_MAX_BYTES = 50 * 1024


def _validate_display_name(name):
    name = (name or "").strip()
    if not name:
        raise ApiError(400, "表示名を入力してください")
    if len(name) > _DISPLAY_NAME_MAX_LEN:
        raise ApiError(400, f"表示名は{_DISPLAY_NAME_MAX_LEN}文字以内にしてください")
    if _DANGEROUS_CHARS_RE.search(name):
        raise ApiError(400, "表示名に使えない文字が含まれています(< > \" ' & は使えません)")
    return name


def _validate_user_id(user_id, users, current_email):
    user_id = (user_id or "").strip().lower()
    if not user_id:
        raise ApiError(400, "ユーザーIDを入力してください")
    if not _USER_ID_RE.match(user_id):
        raise ApiError(400, "ユーザーIDは英数字とアンダースコアのみ、3〜20文字にしてください")
    for other_email, other_user in users.items():
        if other_email != current_email and other_user.get("user_id") == user_id:
            raise ApiError(409, "このユーザーIDは既に使われています")
    return user_id


def _validate_and_process_avatar(avatar_base64):
    """
    data:image/xxx;base64,.... 形式のアイコンを検証する。
    - 50KBを超えるものは拒否
    - 正方形でないものは拒否(PILで実際のサイズを確認)
    - 壊れたデータ/画像として読めないものは拒否
    """
    if not avatar_base64:
        return None
    if not avatar_base64.startswith("data:image/"):
        raise ApiError(400, "アイコンの形式が正しくありません")
    try:
        header, b64_data = avatar_base64.split(",", 1)
    except ValueError:
        raise ApiError(400, "アイコンの形式が正しくありません")
    try:
        raw = base64.b64decode(b64_data)
    except (ValueError, binascii.Error):
        raise ApiError(400, "アイコンのデータが壊れています")
    if len(raw) > _AVATAR_MAX_BYTES:
        raise ApiError(400, f"アイコンは{_AVATAR_MAX_BYTES // 1024}KB以内にしてください")
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))  # verify後は再オープンが必要
        width, height = img.size
    except Exception:
        raise ApiError(400, "アイコンの画像を読み込めませんでした")
    if width != height:
        raise ApiError(400, f"アイコンは正方形の画像にしてください(現在: {width}x{height})")
    return avatar_base64

# ---------- 表向きサイト用のトークン発行ツール ----------
# ちょいツール(表の顔)に置く「トークン発行」用。このサーバーの秘密鍵で署名しているので、
# 見た目はただの文字列でも、このサイト(このサーバー)でしか発行・検証できない
# (秘密鍵を知らない第三者が同じ形式の文字列を偽造することはできない)。
PUBLIC_TOKEN_MAX_AGE = 24 * 3600  # 1日
_public_token_serializer = itsdangerous.URLSafeTimedSerializer(_SESSION_SECRET_KEY, salt="yuzutube-public-token")


def _issue_public_token():
    token_id = secrets.token_hex(8)
    issued_at = time.time()
    token = _public_token_serializer.dumps({"id": token_id, "issued_at": issued_at})
    return {
        "token": token,
        "issued_at": issued_at,
        "expires_at": issued_at + PUBLIC_TOKEN_MAX_AGE,
    }


def _verify_public_token(token):
    try:
        data = _public_token_serializer.loads(token, max_age=PUBLIC_TOKEN_MAX_AGE)
    except itsdangerous.SignatureExpired:
        return {"valid": False, "reason": "expired"}
    except itsdangerous.BadSignature:
        return {"valid": False, "reason": "invalid"}
    remaining = PUBLIC_TOKEN_MAX_AGE - (time.time() - data["issued_at"])
    return {"valid": True, "issued_at": data["issued_at"], "expires_in_seconds": max(0, int(remaining))}


@app.get("/api/token/issue")
def token_issue():
    return jsonify(_issue_public_token())


@app.get("/api/token/verify")
def token_verify():
    token = request.args.get("token", "")
    if not token:
        raise ApiError(400, "token parameter is required")
    return jsonify(_verify_public_token(token))


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

    if _is_ip_banned(client_ip) or _is_email_banned(email):
        # BAN逃れのための新規アカウント作成を防ぐ。理由は詳しく教えない
        # (BANされていること自体は伝えるが、どちらの判定で弾かれたかは教えない)。
        raise ApiError(403, "アカウントを作成できません。お問い合わせフォームからご連絡ください。")

    if not _EMAIL_RE.match(email):
        raise ApiError(400, "メールアドレスの形式が正しくありません")
    if len(password) < 8:
        raise ApiError(400, "パスワードは8文字以上にしてください")
    if not body.get("agreed_to_terms"):
        raise ApiError(400, "利用規約への同意が必要です")

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
    log("auth", f"signup: {email} from {client_ip}", "magenta")

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
        log("auth", f"login failed: {email} from {client_ip}", "red")
        raise ApiError(401, "メールアドレスまたはパスワードが違います")

    user["last_login_ip"] = client_ip
    user["last_login_at"] = time.time()
    _save_users(users)
    log("auth", f"login ok: {email} from {client_ip}", "magenta")

    token = _create_session_token(email)
    return jsonify({"email": email, "token": token, "max_age": SESSION_MAX_AGE})


@app.post("/api/auth/verify")
def auth_verify():
    body = request.get_json(silent=True) or {}
    email = _verify_session_token(body.get("token"))
    if not email:
        raise ApiError(401, "セッションが無効か期限切れです")
    return jsonify({"email": email})


WAKAME_TREND_URL = "https://raw.githubusercontent.com/siawaseok3/wakame/refs/heads/master/trend.json"
WAKAME_TREND_CACHE_TTL_SEC = 3600  # 向こうが1時間おきに更新している旨なので合わせる
_wakame_trend_cache = {"data": None, "fetched_at": 0}
_wakame_trend_lock = threading.Lock()

_ISO8601_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _parse_iso8601_duration(text):
    """trend.jsonのdurationは"PT4M47S"のようなISO 8601形式なので、秒数に変換する。"""
    if not text:
        return None
    m = _ISO8601_DURATION_RE.match(text)
    if not m:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def _parse_int_field(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _convert_wakame_entry(item):
    thumbnails = item.get("thumbnails") or {}
    thumb_url = (
        (thumbnails.get("high") or {}).get("url")
        or (thumbnails.get("medium") or {}).get("url")
        or (thumbnails.get("default") or {}).get("url")
    )
    published_at = (item.get("publishedAt") or "").replace("-", "").replace(":", "")
    upload_date = published_at[:8] if len(published_at) >= 8 else None

    # サムネイル・チャンネルアイコンはbase64化する前の状態でいったん保持しておき、
    # 呼び出し側でまとめて並列変換する(1件ずつ変換すると直列で遅くなるため)。
    return {
        "video_id": item.get("id"),
        "title": item.get("title"),
        "channel": item.get("channel"),
        "channel_id": item.get("channelId"),
        "duration": _parse_iso8601_duration(item.get("duration")),
        "view_count": _parse_int_field(item.get("viewCount")),
        "like_count": _parse_int_field(item.get("likeCount")),
        "comment_count": _parse_int_field(item.get("commentCount")),
        "upload_date": upload_date,
        "url": f"https://www.youtube.com/watch?v={item.get('id')}",
        "_raw_thumbnail_url": thumb_url,
        "_raw_channel_icon_url": item.get("channelIcon"),
    }


def _fetch_wakame_trending():
    """
    表示するトレンド動画は、自前の視聴回数集計(旧方式)ではなく、外部の
    https://github.com/siawaseok3/wakame が1時間おきに更新しているtrend.jsonを
    そのまま使う方式に変更した。サムネイル・チャンネルアイコンはURLのままだと
    ホットリンク周りで壊れることがあるため、base64のdata URIに変換してから返す
    (チャンネルアイコンが「?」のまま出てしまう不具合を避けるため、こちらも
    動画サムネイルと同様に必ず変換する)。
    """
    now = time.time()
    with _wakame_trend_lock:
        cached = _wakame_trend_cache["data"]
        if cached and now - _wakame_trend_cache["fetched_at"] < WAKAME_TREND_CACHE_TTL_SEC:
            return cached

    resp = _fetch_page(WAKAME_TREND_URL, timeout=30)
    if resp.status_code >= 400:
        raise ApiError(502, f"failed to fetch trending source: HTTP {resp.status_code}")
    try:
        raw = json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ApiError(502, f"failed to parse trending source: {e}")

    categories = {
        key: [_convert_wakame_entry(e) for e in (raw.get(key) or [])]
        for key in ("trending", "music", "gaming")
    }

    # 画像変換(base64化)は件数が多い(3カテゴリ分、動画サムネイル+チャンネルアイコンで
    # 最大100件以上になりうる)ので、直列だと遅い。並列で一気に変換する。
    all_entries = [e for entries in categories.values() for e in entries]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        thumb_futures = {executor.submit(_image_to_data_uri, e["_raw_thumbnail_url"]): e for e in all_entries}
        icon_futures = {executor.submit(_image_to_data_uri, e["_raw_channel_icon_url"]): e for e in all_entries}
        for future, entry in thumb_futures.items():
            entry["thumbnail"] = future.result()
        for future, entry in icon_futures.items():
            entry["channel_thumbnail"] = future.result()

    for e in all_entries:
        e.pop("_raw_thumbnail_url", None)
        e.pop("_raw_channel_icon_url", None)

    result = {
        "updated": raw.get("updated"),
        "categories": categories,
    }
    with _wakame_trend_lock:
        _wakame_trend_cache["data"] = result
        _wakame_trend_cache["fetched_at"] = now
    return result


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS site_visit_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("INSERT OR IGNORE INTO site_visit_counter (id, total) VALUES (1, 0)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watch_history (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                thumbnail TEXT,
                channel TEXT,
                channel_id TEXT,
                channel_thumbnail TEXT,
                duration INTEGER,
                view_count INTEGER NOT NULL DEFAULT 0,
                watched_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_history_watched_at ON watch_history (watched_at)")

        # CREATE TABLE IF NOT EXISTS は既存のテーブルの構造までは変更してくれないため、
        # 以前のバージョンで作られた watch_history テーブルに新しいカラムが無い場合は
        # ここで手動で追加する(無いと INSERT 時に "no column named ..." エラーになる)。
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watch_history)")}
        if "email" in existing_cols:
            # さらに古いバージョン(個人アカウント別、PRIMARY KEY (email, video_id))が
            # 残っている場合、カラム追加だけでは主キー構造(video_id単体)まで直せない
            # (ALTER TABLEでは主キーを変更できないため)。この場合はテーブルごと作り直す。
            # 個人別の古い履歴データ自体、今の「サイト全体で共有」の仕様には合わないため、
            # 失っても実害は無い。
            conn.execute("DROP TABLE watch_history")
            conn.execute("""
                CREATE TABLE watch_history (
                    video_id TEXT PRIMARY KEY,
                    title TEXT,
                    thumbnail TEXT,
                    channel TEXT,
                    channel_id TEXT,
                    channel_thumbnail TEXT,
                    duration INTEGER,
                    view_count INTEGER NOT NULL DEFAULT 0,
                    watched_at REAL NOT NULL
                )
            """)
        else:
            if "channel_thumbnail" not in existing_cols:
                conn.execute("ALTER TABLE watch_history ADD COLUMN channel_thumbnail TEXT")
            if "view_count" not in existing_cols:
                conn.execute("ALTER TABLE watch_history ADD COLUMN view_count INTEGER NOT NULL DEFAULT 1")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                email TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel TEXT,
                thumbnail TEXT,
                created_at REAL NOT NULL,
                PRIMARY KEY (email, channel_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_likes (
                email TEXT NOT NULL,
                video_id TEXT NOT NULL,
                title TEXT,
                thumbnail TEXT,
                channel TEXT,
                duration INTEGER,
                created_at REAL NOT NULL,
                PRIMARY KEY (email, video_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inquiries_email ON inquiries (email, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inquiry_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                is_owner INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inquiry_replies_inquiry ON inquiry_replies (inquiry_id, created_at)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_playlists_email ON user_playlists (email, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_playlist_videos (
                playlist_id INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                title TEXT,
                thumbnail TEXT,
                channel TEXT,
                duration INTEGER,
                position INTEGER NOT NULL,
                added_at REAL NOT NULL,
                PRIMARY KEY (playlist_id, video_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_playlist_videos_playlist ON user_playlist_videos (playlist_id, position)")

        # ---------- モデレーション(NGワード・IPバン) ----------
        # NGワードのリストは空のまま出荷する。何が「不適切」かはサイト運営者(オーナー)
        # が管理画面から自分で登録する方針にしている(こちらで初期リストは用意しない)。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE,
                added_by TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_ips (
                ip TEXT PRIMARY KEY,
                reason TEXT,
                banned_by TEXT,
                is_manual INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_emails (
                email TEXT PRIMARY KEY,
                reason TEXT,
                banned_by TEXT,
                is_manual INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS moderation_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ban_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                email TEXT,
                event_type TEXT NOT NULL,
                reason TEXT,
                matched_word TEXT,
                context TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ban_events_ip ON ban_events (ip, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ban_events_email ON ban_events (email, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inquiry_rate_limit (
                identity TEXT PRIMARY KEY,
                count_today INTEGER NOT NULL DEFAULT 0,
                day_key TEXT NOT NULL,
                last_submitted_at REAL NOT NULL
            )
        """)


_init_db()


def _increment_site_visit_counter():
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("UPDATE site_visit_counter SET total = total + 1 WHERE id = 1")
        row = conn.execute("SELECT total FROM site_visit_counter WHERE id = 1").fetchone()
    return row["total"] if row else 0


def _get_site_visit_count():
    with _db() as conn:
        row = conn.execute("SELECT total FROM site_visit_counter WHERE id = 1").fetchone()
    return row["total"] if row else 0


_WATCH_HISTORY_MAX_TOTAL = 200  # サイト全体でこの件数だけ保持する(みんなの視聴履歴)


def _record_watch_history(entry):
    """
    このサイトで実際に見られた動画の履歴。個人アカウントには紐づけない、
    サイト全体で共有される単一の履歴(誰が見たかは記録しない)。
    同じ動画が何度再生されたかも view_count としてカウントする。
    """
    now = time.time()
    video_id = entry.get("video_id")
    if not video_id:
        return
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO watch_history
                (video_id, title, thumbnail, channel, channel_id, channel_thumbnail, duration, view_count, watched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,
                thumbnail=excluded.thumbnail,
                channel=excluded.channel,
                channel_id=excluded.channel_id,
                channel_thumbnail=excluded.channel_thumbnail,
                duration=excluded.duration,
                view_count=watch_history.view_count + 1,
                watched_at=excluded.watched_at
        """, (
            video_id, entry.get("title"), entry.get("thumbnail"), entry.get("channel"),
            entry.get("channel_id"), entry.get("channel_thumbnail"), entry.get("duration"), now,
        ))
        conn.execute("""
            DELETE FROM watch_history WHERE video_id NOT IN (
                SELECT video_id FROM watch_history ORDER BY watched_at DESC LIMIT ?
            )
        """, (_WATCH_HISTORY_MAX_TOTAL,))


def _get_watch_history(limit):
    with _db() as conn:
        rows = conn.execute(
            "SELECT video_id, title, thumbnail, channel, channel_id, channel_thumbnail, duration, "
            "view_count, watched_at FROM watch_history ORDER BY watched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _delete_all_watch_history():
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM watch_history")


def _get_user_subscriptions(email):
    with _db() as conn:
        rows = conn.execute(
            "SELECT channel_id, channel, thumbnail FROM user_subscriptions "
            "WHERE email = ? ORDER BY created_at DESC",
            (email,),
        ).fetchall()
    return [dict(r) for r in rows]


def _add_user_subscription(email, channel_id, channel, thumbnail):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO user_subscriptions (email, channel_id, channel, thumbnail, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email, channel_id) DO UPDATE SET channel=excluded.channel, thumbnail=excluded.thumbnail
        """, (email, channel_id, channel, thumbnail, time.time()))


def _remove_user_subscription(email, channel_id):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM user_subscriptions WHERE email = ? AND channel_id = ?", (email, channel_id))


def _get_user_likes(email):
    with _db() as conn:
        rows = conn.execute(
            "SELECT video_id, title, thumbnail, channel, duration FROM user_likes "
            "WHERE email = ? ORDER BY created_at DESC",
            (email,),
        ).fetchall()
    return [dict(r) for r in rows]


def _add_user_like(email, video_id, title, thumbnail, channel, duration):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO user_likes (email, video_id, title, thumbnail, channel, duration, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email, video_id) DO UPDATE SET
                title=excluded.title, thumbnail=excluded.thumbnail,
                channel=excluded.channel, duration=excluded.duration
        """, (email, video_id, title, thumbnail, channel, duration, time.time()))


def _remove_user_like(email, video_id):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM user_likes WHERE email = ? AND video_id = ?", (email, video_id))


_INQUIRY_SUBJECT_MAX_LEN = 100
_INQUIRY_MESSAGE_MAX_LEN = 2000


def _validate_inquiry_text(value, field_name, max_len):
    if not isinstance(value, str):
        raise ApiError(400, f"{field_name}は文字列で指定してください")
    value = value.strip()
    if not value:
        raise ApiError(400, f"{field_name}を入力してください")
    if len(value) > max_len:
        raise ApiError(400, f"{field_name}は{max_len}文字以内にしてください")
    return value


def _create_inquiry(email, subject, message):
    now = time.time()
    with _CACHE_DB_LOCK, _db() as conn:
        cur = conn.execute(
            "INSERT INTO inquiries (email, subject, message, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (email, subject, message, now),
        )
        return cur.lastrowid


def _get_inquiries_for_user(email):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, subject, status, created_at FROM inquiries WHERE email = ? ORDER BY created_at DESC",
            (email,),
        ).fetchall()
    return [dict(r) for r in rows]


def _get_all_inquiries():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, email, subject, status, created_at FROM inquiries ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_inquiry(inquiry_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT id, email, subject, message, status, created_at FROM inquiries WHERE id = ?",
            (inquiry_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_inquiry_replies(inquiry_id):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, email, is_owner, message, created_at FROM inquiry_replies "
            "WHERE inquiry_id = ? ORDER BY created_at ASC",
            (inquiry_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _add_inquiry_reply(inquiry_id, email, is_owner, message):
    now = time.time()
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute(
            "INSERT INTO inquiry_replies (inquiry_id, email, is_owner, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (inquiry_id, email, 1 if is_owner else 0, message, now),
        )
        # オーナーが返信したら「対応中」、本人が追記したら「返信待ち」のように状態を更新しておく
        new_status = "answered" if is_owner else "open"
        conn.execute("UPDATE inquiries SET status = ? WHERE id = ?", (new_status, inquiry_id))


def _delete_inquiry(inquiry_id):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM inquiry_replies WHERE inquiry_id = ?", (inquiry_id,))
        conn.execute("DELETE FROM inquiries WHERE id = ?", (inquiry_id,))


_PLAYLIST_MAX_COUNT_PER_USER = 10
_PLAYLIST_NAME_MAX_LEN = 20
_PLAYLIST_MAX_VIDEOS = 100


def _validate_playlist_name(name):
    if not isinstance(name, str):
        raise ApiError(400, "プレイリスト名は文字列で指定してください")
    name = name.strip()
    if not name:
        raise ApiError(400, "プレイリスト名を入力してください")
    if len(name) > _PLAYLIST_NAME_MAX_LEN:
        raise ApiError(400, f"プレイリスト名は{_PLAYLIST_NAME_MAX_LEN}文字以内にしてください")
    return name


def _get_user_playlists(email):
    with _db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.created_at, COUNT(v.video_id) AS video_count
            FROM user_playlists p
            LEFT JOIN user_playlist_videos v ON v.playlist_id = p.id
            WHERE p.email = ?
            GROUP BY p.id
            ORDER BY p.created_at DESC
        """, (email,)).fetchall()
    return [dict(r) for r in rows]


def _get_playlist(playlist_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT id, email, name, created_at FROM user_playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_playlist_videos(playlist_id):
    with _db() as conn:
        rows = conn.execute(
            "SELECT video_id, title, thumbnail, channel, duration, position FROM user_playlist_videos "
            "WHERE playlist_id = ? ORDER BY position ASC",
            (playlist_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _create_playlist(email, name):
    with _CACHE_DB_LOCK, _db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_playlists WHERE email = ?", (email,)
        ).fetchone()["c"]
        if count >= _PLAYLIST_MAX_COUNT_PER_USER:
            raise ApiError(409, f"プレイリストは{_PLAYLIST_MAX_COUNT_PER_USER}個までしか作成できません")
        cur = conn.execute(
            "INSERT INTO user_playlists (email, name, created_at) VALUES (?, ?, ?)",
            (email, name, time.time()),
        )
        return cur.lastrowid


def _rename_playlist(playlist_id, name):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("UPDATE user_playlists SET name = ? WHERE id = ?", (name, playlist_id))


def _delete_playlist(playlist_id):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM user_playlist_videos WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM user_playlists WHERE id = ?", (playlist_id,))


def _add_video_to_playlist(playlist_id, video_id, title, thumbnail, channel, duration):
    with _CACHE_DB_LOCK, _db() as conn:
        existing = conn.execute(
            "SELECT video_id FROM user_playlist_videos WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        ).fetchone()
        if existing:
            return  # 既に入っているなら何もしない(エラーにはしない)
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_playlist_videos WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()["c"]
        if count >= _PLAYLIST_MAX_VIDEOS:
            raise ApiError(409, f"このプレイリストには{_PLAYLIST_MAX_VIDEOS}本までしか追加できません")
        conn.execute(
            "INSERT INTO user_playlist_videos (playlist_id, video_id, title, thumbnail, channel, duration, position, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (playlist_id, video_id, title, thumbnail, channel, duration, count, time.time()),
        )


def _remove_video_from_playlist(playlist_id, video_id):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute(
            "DELETE FROM user_playlist_videos WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )


# ---------- モデレーション ----------

def _get_banned_words():
    with _db() as conn:
        rows = conn.execute("SELECT id, word, added_by, created_at FROM banned_words ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def _add_banned_word(word, added_by):
    word = (word or "").strip().lower()
    if not word:
        raise ApiError(400, "word is required")
    if len(word) < 2:
        raise ApiError(400, "word must be at least 2 characters (1文字だけだと誤検知が多すぎるため)")
    if len(word) > 100:
        raise ApiError(400, "word must be 100 characters or fewer")
    with _CACHE_DB_LOCK, _db() as conn:
        try:
            conn.execute(
                "INSERT INTO banned_words (word, added_by, created_at) VALUES (?, ?, ?)",
                (word, added_by, time.time()),
            )
        except sqlite3.IntegrityError:
            pass  # 既に登録済みなら何もしない


def _remove_banned_word(word_id):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM banned_words WHERE id = ?", (word_id,))


def _remove_banned_word_by_text(word):
    word = (word or "").strip().lower()
    if not word:
        return False
    with _CACHE_DB_LOCK, _db() as conn:
        cur = conn.execute("DELETE FROM banned_words WHERE word = ?", (word,))
        return cur.rowcount > 0


def _clear_all_banned_words():
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM banned_words")


def _count_banned_words():
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM banned_words").fetchone()
    return row["c"] if row else 0


def _find_matched_banned_word(text):
    """
    textの中にNGワードが含まれていれば、そのワードを返す(無ければNone)。

    単純な部分一致だと、NGワードがたまたま別の単語の一部として含まれているだけで
    誤検知してしまう(いわゆる「Scunthorpe問題」)。英数字だけのワードは単語境界
    (前後が英数字で連続していない)で区切られている時だけマッチとみなすことで、
    これをかなり減らせる。日本語混じりのワードは単語境界という概念が無いため
    部分一致のままだが、1文字だけのような極端に短いワードは誤検知が多すぎるので
    対象外にする(登録時にも2文字未満は弾いている)。
    """
    if not text:
        return None
    lowered = text.lower()
    words = _get_banned_words()
    for w in words:
        word = w["word"]
        if not word:
            continue
        if word.isascii():
            pattern = r"\b" + re.escape(word) + r"\b"
            if re.search(pattern, lowered):
                return word
        else:
            if len(word) < 2:
                continue
            if word in lowered:
                return word
    return None


def _check_video_moderation_or_ban(video_id, data):
    """
    動画の「タイトル」にNGワードが含まれていたら、視聴者(のIPと、ログイン中なら
    メールアドレスも)を自動BANする。/api/stream と /api/info の両方から共通で使う。

    説明文までは見ない。説明文には「この動画には〇〇的な内容は含まれません」の
    ような注意書き・免責事項が書かれていることがあり、そうした文が誤ってNGワードに
    ヒットして無関係な動画まで誤BANしてしまうことがあるため。タイトルの方が
    実際のコンテンツを直接的に表しているので、判定材料として使う。

    NGワードに引っかからなかった場合、Groq(設定されていれば)にもタイトルを
    判定させる。こちらも設定・API呼び出し失敗時は何もしない(fail open)。
    """
    title = data.get("title") or ""
    matched_word = _find_matched_banned_word(title)
    if matched_word:
        client_ip = _get_client_ip()
        context = f"video_id={video_id}"
        _ban_ip(client_ip, "NGワードを含む動画の視聴", matched_word=matched_word, context=context)
        token = request.headers.get("X-Session-Token", "")
        email = _verify_session_token(token) if token else None
        if email:
            _ban_email(email, "NGワードを含む動画の視聴", matched_word=matched_word, context=context, ip=client_ip)
        log("denied", f"NGワード動画の視聴によりBAN: ip={client_ip} email={email} (word={matched_word}, video={video_id})", "red")
        raise ApiError(403, "利用を制限されました。お問い合わせフォームからご連絡ください。")

    groq_result = _check_title_with_groq(title)
    if groq_result and groq_result[0]:
        is_bad, reason = groq_result
        client_ip = _get_client_ip()
        context = f"video_id={video_id} / AI理由: {reason}"
        _ban_ip(client_ip, "AI判定により不適切と判断された動画の視聴", context=context)
        token = request.headers.get("X-Session-Token", "")
        email = _verify_session_token(token) if token else None
        if email:
            _ban_email(email, "AI判定により不適切と判断された動画の視聴", context=context, ip=client_ip)
        log("denied", f"AI判定によりBAN: ip={client_ip} email={email} (video={video_id}, reason={reason})", "red")
        raise ApiError(403, "利用を制限されました。お問い合わせフォームからご連絡ください。")


def _log_ban_event(ip, email, event_type, reason=None, matched_word=None, context=None):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute(
            "INSERT INTO ban_events (ip, email, event_type, reason, matched_word, context, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ip, email, event_type, reason, matched_word, context, time.time()),
        )


def _is_ip_banned(ip):
    if not ip:
        return None
    with _db() as conn:
        row = conn.execute("SELECT ip, reason, is_manual, created_at FROM banned_ips WHERE ip = ?", (ip,)).fetchone()
    return dict(row) if row else None


def _ban_ip(ip, reason, banned_by=None, is_manual=False, email=None, matched_word=None, context=None):
    """
    IPだけでなく、分かっている場合(ログイン中に違反した等)はemailも同時にBANする。
    片方だけ解除して抜け道にならないよう、基本的にセットで扱う。
    """
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO banned_ips (ip, reason, banned_by, is_manual, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET reason=excluded.reason, banned_by=excluded.banned_by, is_manual=excluded.is_manual
        """, (ip, reason, banned_by, 1 if is_manual else 0, time.time()))
    _log_ban_event(ip, email, "manual_ban" if is_manual else "auto_ban", reason=reason, matched_word=matched_word, context=context)
    if email:
        _ban_email(email, reason, banned_by=banned_by, is_manual=is_manual, ip=ip, matched_word=matched_word, context=context)


def _unban_ip(ip, unbanned_by=None):
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM banned_ips WHERE ip = ?", (ip,))
    _log_ban_event(ip, None, "unban", reason=f"unbanned by {unbanned_by}" if unbanned_by else None)


def _get_all_banned_ips():
    with _db() as conn:
        rows = conn.execute("SELECT ip, reason, banned_by, is_manual, created_at FROM banned_ips ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def _is_email_banned(email):
    if not email:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT email, reason, is_manual, created_at FROM banned_emails WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def _ban_email(email, reason, banned_by=None, is_manual=False, ip=None, matched_word=None, context=None):
    email = (email or "").strip().lower()
    if not email:
        return
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO banned_emails (email, reason, banned_by, is_manual, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET reason=excluded.reason, banned_by=excluded.banned_by, is_manual=excluded.is_manual
        """, (email, reason, banned_by, 1 if is_manual else 0, time.time()))
    _log_ban_event(ip or "-", email, "manual_ban" if is_manual else "auto_ban", reason=reason, matched_word=matched_word, context=context)


def _unban_email(email, unbanned_by=None):
    email = (email or "").strip().lower()
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("DELETE FROM banned_emails WHERE email = ?", (email,))
    _log_ban_event("-", email, "unban", reason=f"unbanned by {unbanned_by}" if unbanned_by else None)


def _get_all_banned_emails():
    with _db() as conn:
        rows = conn.execute("SELECT email, reason, banned_by, is_manual, created_at FROM banned_emails ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# ---------- AIによる動画タイトル判定(任意機能、Groq) ----------

def _get_moderation_policy():
    """
    「何を不適切とみなすか」の基準文。NGワードと同じ思想で、こちらでは
    一切決めずオーナーが管理画面から設定する。未設定ならAI判定自体を行わない。
    """
    with _db() as conn:
        row = conn.execute("SELECT value FROM moderation_settings WHERE key = 'moderation_policy'").fetchone()
    return row["value"] if row and row["value"] else ""


def _set_moderation_policy(policy):
    policy = (policy or "").strip()
    if len(policy) > 2000:
        raise ApiError(400, "判定基準は2000文字以内にしてください")
    with _CACHE_DB_LOCK, _db() as conn:
        conn.execute("""
            INSERT INTO moderation_settings (key, value) VALUES ('moderation_policy', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (policy,))


def _check_title_with_groq(title):
    """
    動画タイトルをGroq(OpenAI互換のchat completions API)に判定させる。
    設定(APIキー・判定基準)が無ければ何もせずNoneを返す
    (=AI判定を使わない、NGワードだけで運用する)。
    API呼び出し自体が失敗した場合も、誤って利用者をBANしないようNoneを返す
    (fail open)。戻り値は (不適切かどうか, 判定理由) のタプル、またはNone。
    """
    policy = _get_moderation_policy()
    if not GROQ_API_KEY or not policy or not title:
        return None

    system_prompt = (
        "あなたは動画共有サイトのコンテンツ審査担当です。与えられた「判定基準」に照らして、"
        "動画タイトルが基準に該当する不適切なものかどうかを判定してください。"
        '必ず次のJSON形式のみで回答してください(他の文章は一切含めないこと): '
        '{"inappropriate": true または false, "reason": "短い理由"}'
    )
    user_prompt = f"判定基準:\n{policy}\n\n動画タイトル: {title}"

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            log("access", f"Groq API呼び出し失敗(HTTP {resp.status_code})、AI判定はスキップします", "yellow")
            return None
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        parsed = json.loads(text)
        return bool(parsed.get("inappropriate")), str(parsed.get("reason") or "")
    except Exception as e:
        log("access", f"Groq判定でエラーが発生したためスキップします: {e}", "yellow")
        return None


def _get_ban_events(ip=None, email=None, limit=100):
    query = "SELECT id, ip, email, event_type, reason, matched_word, context, created_at FROM ban_events"
    conditions = []
    params = []
    if ip:
        conditions.append("ip = ?")
        params.append(ip)
    if email:
        conditions.append("email = ?")
        params.append(email)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


_INQUIRY_RATE_LIMIT_MAX_PER_DAY = 5
_INQUIRY_RATE_LIMIT_COOLDOWN_SEC = 60  # 連続送信を防ぐ、最低これだけ間隔を空ける


def _check_and_bump_inquiry_rate_limit(identity):
    """お問い合わせの連投・荒らし対策。1日あたりの上限とクールタイムの両方を見る。
    上限を超えている場合はApiErrorを投げる(呼び出し側で握りつぶさないこと)。"""
    day_key = datetime.now().strftime("%Y-%m-%d")
    now = time.time()
    with _CACHE_DB_LOCK, _db() as conn:
        row = conn.execute(
            "SELECT count_today, day_key, last_submitted_at FROM inquiry_rate_limit WHERE identity = ?",
            (identity,),
        ).fetchone()

        if row and row["day_key"] == day_key:
            if now - row["last_submitted_at"] < _INQUIRY_RATE_LIMIT_COOLDOWN_SEC:
                wait = int(_INQUIRY_RATE_LIMIT_COOLDOWN_SEC - (now - row["last_submitted_at"]))
                raise ApiError(429, f"連続で送信することはできません。{wait}秒待ってから再度お試しください")
            if row["count_today"] >= _INQUIRY_RATE_LIMIT_MAX_PER_DAY:
                raise ApiError(429, f"お問い合わせは1日{_INQUIRY_RATE_LIMIT_MAX_PER_DAY}件までです。また明日お試しください")
            new_count = row["count_today"] + 1
        else:
            new_count = 1

        conn.execute("""
            INSERT INTO inquiry_rate_limit (identity, count_today, day_key, last_submitted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(identity) DO UPDATE SET count_today=excluded.count_today, day_key=excluded.day_key, last_submitted_at=excluded.last_submitted_at
        """, (identity, new_count, day_key, now))


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


def _cleanup_expired_response_cache():
    threshold = time.time() - RESPONSE_CACHE_TTL_SECONDS
    with _CACHE_DB_LOCK, _db() as conn:
        cur = conn.execute("DELETE FROM response_cache WHERE created_at < ?", (threshold,))
        return cur.rowcount


_deleted_on_startup = _cleanup_expired_response_cache()
if _deleted_on_startup:
    log("cache", f"起動時クリーンアップ: 期限切れキャッシュ {_deleted_on_startup} 件を削除しました", "cyan")



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


if PROXY_URL:
    _proxy_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
    )
    _urlopen = _proxy_opener.open
else:
    _urlopen = urllib.request.urlopen


def _fetch_page(url, timeout=60):
    """
    YouTubeの生HTMLページを取ってくる。requestsではなくPython標準ライブラリ(urllib)を
    使っている。requests(urllib3)だとブロックされるケースでも、urllibだとTLS/HTTPの
    フィンガープリントが変わって通ることがあるため、生スクレイピング系はこちらに統一した。
    cookies.txtが置いてあれば、そのcookieも一緒に送る(consent回避用のCONSENT/SOCSは常に付与)。
    PROXY_URLが設定されていれば、そのプロキシ経由でリクエストする。
    """
    headers = dict(_PAGE_HEADERS)
    headers["Cookie"] = _cookie_header_string()
    req = urllib.request.Request(url, headers=headers)
    content_encoding = ""
    try:
        with _urlopen(req, timeout=timeout) as resp:
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


def _ydl_opts(extra=None, cookiefile_override=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"lang": ["ja"], "formats": ["missing_pot"]}},
        # ライブ配信で"No video formats found!"エラーになることがある既知のyt-dlp側の問題
        # (PO Token絡みでフォーマットが弾かれてしまう)への対策。missing_potで弾かれた
        # フォーマットも許可しつつ、それでも見つからない場合はエラーで落とさず、
        # manifest_url(HLSのマスタープレイリストURL)だけでも拾えるようにする。
        "ignore_no_formats_error": True,
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
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    chosen_cookiefile = cookiefile_override if cookiefile_override is not None else COOKIES_FILE_PATH
    if chosen_cookiefile and os.path.isfile(chosen_cookiefile):
        opts["cookiefile"] = chosen_cookiefile
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


_NETWORK_ERROR_HINTS = (
    "urlopen error", "connection refused", "network is unreachable",
    "temporary failure in name resolution", "timed out", "timeout",
    "failed to resolve", "connection reset", "no route to host",
    "unreachable", "eof occurred in violation of protocol",
    "connection aborted", "remote end closed connection",
)


def _looks_like_network_error(message):
    lowered = message.lower()
    return any(hint in lowered for hint in _NETWORK_ERROR_HINTS)


_AUTH_ERROR_HINTS = (
    "sign in", "confirm you're not a bot", "confirm you are not a bot",
    "requested format is not available", "この操作を続行するには", "ログインして",
    "bot ではないこと", "not a bot",
)


def _looks_like_auth_or_format_error(message):
    """
    bot判定・フォーマット取得失敗は、多くの場合そのアカウント(cookie)の状態に
    起因する。別のcookieに切り替えると解決することがあるため、判定して
    「次のcookieを試す」トリガーに使う。
    """
    lowered = message.lower()
    return any(hint.lower() in lowered for hint in _AUTH_ERROR_HINTS)


def _extract(source_url, extra_opts=None, retries=2, retry_delay=1.5, max_cookie_attempts=None, use_cookies=True):
    """
    VPN切断・回線の瞬断など「サーバー側は悪くないが一時的にネットが繋がらない」ケースを
    ある程度自動で吸収するため、ネットワーク起因のエラーだけ数回リトライする。
    それでもダメならHTTP 400ではなく503(Service Unavailable)を返す
    (400は「リクエスト自体がおかしい」という意味なので、一時的な接続断には不適切)。
    動画が存在しない・非公開などの本当のエラーは、リトライせず通常通り400のまま。

    bot判定/フォーマット取得失敗の場合は、用意されている複数のcookies.txt
    (cookies.txt, cookies2.txt, ...)を順番に試す。1つのアカウントの調子が
    悪くても、他のアカウントのcookieで通ることがあるため。

    use_cookies=False にすると、Cookieを一切使わずに抽出する(高速パス専用)。
    実測で、Cookieを渡すとyt-dlpがログイン済み向けの別クライアント(tv_downgraded等)に
    切り替わり、Node.js無しの状態だとそちらでは映像+音声一体のフォーマットが
    手に入らず失敗する、ということが確認できたための対応
    (compare_extraction.pyでの検証結果、2026-08-09)。
    """
    if not use_cookies:
        cookie_candidates = [None]
    else:
        cookie_candidates = COOKIES_FILE_PATHS or [None]
        if max_cookie_attempts:
            cookie_candidates = cookie_candidates[:max_cookie_attempts]
    last_error = None

    for cookiefile in cookie_candidates:
        for attempt in range(retries + 1):
            try:
                with yt_dlp.YoutubeDL(_ydl_opts(extra_opts, cookiefile_override=cookiefile)) as ydl:
                    return ydl.extract_info(source_url, download=False)
            except yt_dlp.utils.DownloadError as e:
                message = str(e)
                if _looks_like_network_error(message):
                    last_error = message
                    if attempt < retries:
                        time.sleep(retry_delay)
                    continue
                if _looks_like_auth_or_format_error(message) and cookiefile != cookie_candidates[-1]:
                    # このcookieでは通らなかった。次のcookieに切り替える。
                    last_error = message
                    break
                raise ApiError(400, f"yt-dlp error: {message}")
        else:
            # ネットワークエラーでretries回とも失敗し、forがbreakせず終わった場合は
            # このcookieでの再試行を使い切ったとみなし、次のcookieには進まず打ち切る。
            raise ApiError(503, f"ネットワーク接続が不安定です(VPN切断等の可能性)。しばらくしてから再度お試しください: {last_error}")

    raise ApiError(400, f"yt-dlp error: {last_error}")


_RAW_EXTRACT_CACHE_TTL_SEC = 120  # /api/info と /api/stream が同じ動画を数秒差で
                                   # 呼んだ時に、重いyt-dlp抽出(Node実行含む)を
                                   # 2回やらずに済むようにするための短いキャッシュ
_raw_extract_cache = {}
_raw_extract_cache_lock = threading.Lock()


def _extract_full(video_id):
    """フォーマットを絞らず全情報(formats一覧込み)を取得しつつ、一覧用インデックスにも書き込む。
    /api/info と /api/stream はほぼ同時に同じ動画に対して呼ばれることが多いため、
    直近の抽出結果を短時間だけメモリに残しておき、2回とも一からNode.jsでの
    署名解読をやり直す無駄を避けている。"""
    now = time.time()
    with _raw_extract_cache_lock:
        cached = _raw_extract_cache.get(video_id)
        if cached and now - cached[1] < _RAW_EXTRACT_CACHE_TTL_SEC:
            return cached[0]

    source_url = _resolve_url(video_id)
    data = _extract(source_url)
    _cache_upsert(video_id, data)

    with _raw_extract_cache_lock:
        _raw_extract_cache[video_id] = (data, now)
        if len(_raw_extract_cache) > 200:
            oldest_ids = sorted(_raw_extract_cache, key=lambda k: _raw_extract_cache[k][1])[:50]
            for old_id in oldest_ids:
                del _raw_extract_cache[old_id]

    return data


_FAST_EXTRACT_TIMEOUT_SEC = 8


def _extract_fast(video_id):
    """
    Node.js(署名解読)を無効化した状態で動画情報を取る。yt-dlpは、Node.js等の
    JavaScriptランタイムが使えない場合、自動的にJS不要なクライアント(android_vr等、
    バージョンによって組み合わせは変わる)にフォールバックしてくれる。これは
    こちらでクライアントを決め打ちするより、a-Shell(端末にNode.js自体が無い環境)で
    確認した時の挙動に忠実で、かつyt-dlpのバージョンが上がってクライアントの
    組み合わせが変わっても追従できる。
    通常のフォーマット取得(Node.jsでの署名解読あり)より圧倒的に速いが、
    低画質(360p相当)のフォーマットしか手に入らないことが多い。動画ページを開いた
    直後、まず低画質でもいいのですぐ再生を始めたい場合に使う。
    失敗した・実際に再生可能なフォーマット(映像+音声が一体のもの、またはHLS)が
    1つも無かった場合はNoneを返す(呼び出し側で通常の方式にフォールバックすること)。
    formatsキー自体が空でないだけでは不十分(映像だけ・音声だけの分離フォーマットしか
    無いこともあり、それだと自作プレイヤー側では再生できないため、事前に弾いておく)。
    """
    try:
        source_url = _resolve_url(video_id)
        data = _extract(
            source_url,
            extra_opts={"js_runtimes": {}, "remote_components": []},
            retries=0,
            use_cookies=False,
        )
    except Exception as e:
        log("access", f"高速パス失敗(video_id={video_id}): {e}", "yellow")
        return None
    if not data or not data.get("formats"):
        log("access", f"高速パス: フォーマットが1つも無かった(video_id={video_id})", "yellow")
        return None

    has_playable = any(
        f.get("url") and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
        for f in data["formats"]
    )
    if not has_playable and not data.get("manifest_url"):
        log("access", f"高速パス: 再生可能な(映像+音声一体の)フォーマットが無かった(video_id={video_id})", "yellow")
        return None
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
        "extractor_args": {"youtube": {"lang": ["ja"], "formats": ["missing_pot"]}},
        # ライブ配信で"No video formats found!"エラーになることがある既知のyt-dlp側の問題
        # (PO Token絡みでフォーマットが弾かれてしまう)への対策。missing_potで弾かれた
        # フォーマットも許可しつつ、それでも見つからない場合はエラーで落とさず、
        # manifest_url(HLSのマスタープレイリストURL)だけでも拾えるようにする。
        "ignore_no_formats_error": True,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
        "http_headers": {
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": _PAGE_HEADERS["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
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


_PLAYLIST_ID_PREFIXES = ("PL", "UU", "LL", "WL", "FL", "RD", "OL")


def _slim_entry(e):
    """検索結果/プレイリスト/チャンネル一覧の1件分を整形する共通ヘルパー。
    チャンネルの「再生リスト」タブ等では、1件が動画ではなく再生リストそのもの
    (idが動画IDではなく再生リストID)であることがあるため、判別できるようにしている。"""
    thumbnails = e.get("thumbnails") or []
    thumbnail = thumbnails[-1].get("url") if thumbnails else e.get("thumbnail")
    entry_id = e.get("id") or ""
    is_playlist = e.get("_type") == "playlist" or (
        isinstance(entry_id, str) and entry_id.startswith(_PLAYLIST_ID_PREFIXES) and len(entry_id) > 12
    )
    return {
        "video_id": entry_id,
        "title": e.get("title"),
        "url": e.get("url") or e.get("webpage_url"),
        "duration": e.get("duration"),
        "view_count": e.get("view_count"),
        "channel": e.get("channel") or e.get("uploader"),
        "channel_id": e.get("channel_id") or e.get("uploader_id"),
        "thumbnail": thumbnail,
        "live_status": e.get("live_status"),
        "upload_date": e.get("upload_date"),
        "entry_type": "playlist" if is_playlist else "video",
        "video_count": e.get("playlist_count") if is_playlist else None,
    }




@app.post("/api/visit")
def record_visit():
    """フロントエンドがページ表示のたびに1回だけ叩く。サイト全体の累計閲覧数を1増やす。"""
    total = _increment_site_visit_counter()
    return jsonify({"total": total})


@app.get("/api/visit")
def get_visit_count():
    return jsonify({"total": _get_site_visit_count()})


@app.post("/api/history")
def post_history():
    """
    「みんなの視聴履歴」に1件記録する。個人アカウントには紐づけない
    (誰が見たかは記録しない、サイト全体で共有される単一のフィード)。
    """
    body = request.get_json(silent=True) or {}
    video_id = body.get("video_id")
    if not video_id:
        raise ApiError(400, "video_id is required")
    _record_watch_history(body)
    return jsonify({"ok": True})


@app.get("/api/history")
def get_history():
    limit = max(1, min(int(request.args.get("limit", 100)), 200))
    return jsonify({"entries": _get_watch_history(limit)})


@app.delete("/api/history")
def delete_history():
    """みんなの視聴履歴を全消去する。破壊的操作なので管理者パスワードが必要。"""
    _require_admin_password()
    _delete_all_watch_history()
    return jsonify({"ok": True})


def _authed_email():
    token = request.headers.get("X-Session-Token", "")
    email = _verify_session_token(token)
    if not email:
        raise ApiError(401, "ログインが必要です")
    return email


def _optional_authed_email():
    """ログインしていなくてもエラーにしない版。検索・動画視聴のように、
    ログイン任意の機能で「もしログインしていればemailも記録したい」場合に使う。"""
    token = request.headers.get("X-Session-Token", "")
    return _verify_session_token(token)


@app.get("/api/user/me")
def get_my_profile():
    email = _authed_email()
    users = _load_users()
    user = users.get(email) or {}
    return jsonify({
        "email": email,
        "display_name": user.get("display_name", ""),
        "user_id": user.get("user_id", ""),
        "avatar_base64": user.get("avatar_base64"),
        "is_owner": email in OWNER_EMAILS,
    })


@app.put("/api/user/me")
def update_my_profile():
    email = _authed_email()
    body = request.get_json(silent=True) or {}

    with _AUTH_LOCK:
        users = _load_users()
        if email not in users:
            raise ApiError(404, "ユーザーが見つかりません")

        display_name = _validate_display_name(body.get("display_name"))
        user_id = _validate_user_id(body.get("user_id"), users, email)

        # アイコンは、送られてきた時だけ検証して更新する。
        # フィールド自体が無い(変更しない)場合は既存のものをそのまま維持する。
        # 明示的に空文字("")が送られてきた場合はアイコンを削除する。
        if "avatar_base64" in body:
            new_avatar = body.get("avatar_base64") or ""
            if new_avatar:
                # 変更のたびに前のアイコンのデータは残さず、新しいものだけを保持する
                # (base64をレコードに直接持たせているだけなので、上書きが即クリーンアップになる)
                users[email]["avatar_base64"] = _validate_and_process_avatar(new_avatar)
            else:
                users[email]["avatar_base64"] = None

        users[email]["display_name"] = display_name
        users[email]["user_id"] = user_id
        _save_users(users)

    return jsonify({
        "email": email,
        "display_name": display_name,
        "user_id": user_id,
        "avatar_base64": users[email].get("avatar_base64"),
    })


@app.post("/api/inquiries")
def create_inquiry_endpoint():
    email = _authed_email()
    _check_and_bump_inquiry_rate_limit(email)
    body = request.get_json(silent=True) or {}
    subject = _validate_inquiry_text(body.get("subject"), "件名", _INQUIRY_SUBJECT_MAX_LEN)
    message = _validate_inquiry_text(body.get("message"), "本文", _INQUIRY_MESSAGE_MAX_LEN)
    inquiry_id = _create_inquiry(email, subject, message)
    return jsonify({"id": inquiry_id, "ok": True})


@app.get("/api/inquiries")
def list_inquiries_endpoint():
    """
    一般ユーザーは自分が送ったお問い合わせだけ、オーナーは全員分を見られる。
    お問い合わせ対応ができるのはオーナーだけ、という要件をここで担保している。
    """
    email = _authed_email()
    if email in OWNER_EMAILS:
        return jsonify({"inquiries": _get_all_inquiries(), "is_owner": True})
    return jsonify({"inquiries": _get_inquiries_for_user(email), "is_owner": False})


@app.get("/api/inquiries/<int:inquiry_id>")
def get_inquiry_endpoint(inquiry_id):
    email = _authed_email()
    inquiry = _get_inquiry(inquiry_id)
    if not inquiry:
        raise ApiError(404, "お問い合わせが見つかりません")
    is_owner = email in OWNER_EMAILS
    if not is_owner and inquiry["email"] != email:
        # 他人のお問い合わせは、オーナーでない限り見えない(存在の有無も分からないよう404にする)
        raise ApiError(404, "お問い合わせが見つかりません")
    replies = _get_inquiry_replies(inquiry_id)

    # LINEのような見た目にするため、発言者ごとのアイコン・表示名も一緒に返す
    users = _load_users()
    participant_emails = {inquiry["email"]} | {r["email"] for r in replies}
    avatars = {}
    for p_email in participant_emails:
        u = users.get(p_email) or {}
        avatars[p_email] = {
            "avatar_base64": u.get("avatar_base64"),
            "display_name": u.get("display_name") or "",
        }

    return jsonify({"inquiry": inquiry, "replies": replies, "is_owner": is_owner, "avatars": avatars})


@app.post("/api/inquiries/<int:inquiry_id>/replies")
def reply_inquiry_endpoint(inquiry_id):
    email = _authed_email()
    inquiry = _get_inquiry(inquiry_id)
    if not inquiry:
        raise ApiError(404, "お問い合わせが見つかりません")
    is_owner = email in OWNER_EMAILS
    if not is_owner and inquiry["email"] != email:
        raise ApiError(404, "お問い合わせが見つかりません")
    body = request.get_json(silent=True) or {}
    message = _validate_inquiry_text(body.get("message"), "本文", _INQUIRY_MESSAGE_MAX_LEN)
    _add_inquiry_reply(inquiry_id, email, is_owner, message)
    return jsonify({"ok": True})


@app.delete("/api/inquiries/<int:inquiry_id>")
def delete_inquiry_endpoint(inquiry_id):
    """お問い合わせを削除する。オーナーだけができる破壊的操作。"""
    email = _authed_email()
    if email not in OWNER_EMAILS:
        raise ApiError(403, "オーナーのみ削除できます")
    inquiry = _get_inquiry(inquiry_id)
    if not inquiry:
        raise ApiError(404, "お問い合わせが見つかりません")
    _delete_inquiry(inquiry_id)
    return jsonify({"ok": True})


def _get_owned_playlist_or_404(playlist_id, email):
    playlist = _get_playlist(playlist_id)
    if not playlist or playlist["email"] != email:
        raise ApiError(404, "プレイリストが見つかりません")
    return playlist


@app.get("/api/playlists")
def list_playlists_endpoint():
    email = _authed_email()
    return jsonify({"playlists": _get_user_playlists(email), "max_count": _PLAYLIST_MAX_COUNT_PER_USER})


@app.post("/api/playlists")
def create_playlist_endpoint():
    email = _authed_email()
    body = request.get_json(silent=True) or {}
    name = _validate_playlist_name(body.get("name"))
    playlist_id = _create_playlist(email, name)
    return jsonify({"id": playlist_id, "name": name})


@app.get("/api/playlists/<int:playlist_id>")
def get_playlist_endpoint(playlist_id):
    email = _authed_email()
    playlist = _get_owned_playlist_or_404(playlist_id, email)
    videos = _get_playlist_videos(playlist_id)
    return jsonify({"playlist": playlist, "videos": videos, "max_videos": _PLAYLIST_MAX_VIDEOS})


@app.put("/api/playlists/<int:playlist_id>")
def rename_playlist_endpoint(playlist_id):
    email = _authed_email()
    _get_owned_playlist_or_404(playlist_id, email)
    body = request.get_json(silent=True) or {}
    name = _validate_playlist_name(body.get("name"))
    _rename_playlist(playlist_id, name)
    return jsonify({"ok": True, "name": name})


@app.delete("/api/playlists/<int:playlist_id>")
def delete_playlist_endpoint(playlist_id):
    email = _authed_email()
    _get_owned_playlist_or_404(playlist_id, email)
    _delete_playlist(playlist_id)
    return jsonify({"ok": True})


@app.post("/api/playlists/<int:playlist_id>/videos")
def add_playlist_video_endpoint(playlist_id):
    email = _authed_email()
    _get_owned_playlist_or_404(playlist_id, email)
    body = request.get_json(silent=True) or {}
    video_id = body.get("video_id")
    if not video_id:
        raise ApiError(400, "video_id is required")
    _add_video_to_playlist(
        playlist_id, video_id, body.get("title"), body.get("thumbnail"), body.get("channel"), body.get("duration")
    )
    return jsonify({"ok": True})


@app.delete("/api/playlists/<int:playlist_id>/videos/<video_id>")
def remove_playlist_video_endpoint(playlist_id, video_id):
    email = _authed_email()
    _get_owned_playlist_or_404(playlist_id, email)
    _remove_video_from_playlist(playlist_id, video_id)
    return jsonify({"ok": True})


def _require_owner():
    email = _authed_email()
    if email not in OWNER_EMAILS:
        raise ApiError(403, "オーナーのみアクセスできます")
    return email


@app.get("/api/admin/banned-words")
def list_banned_words_endpoint():
    _require_owner()
    words = _get_banned_words()
    return jsonify({"words": words, "count": len(words)})


@app.delete("/api/admin/banned-words/by-text")
def remove_banned_word_by_text_endpoint():
    """
    一覧を見せない運用なので、削除したい単語をそのまま入力してもらって
    テキスト一致で削除する。IDが分からなくても削除できるようにするため。
    """
    _require_owner()
    body = request.get_json(silent=True) or {}
    removed = _remove_banned_word_by_text(body.get("word"))
    if not removed:
        raise ApiError(404, "指定した単語は登録されていません")
    return jsonify({"ok": True})


@app.delete("/api/admin/banned-words")
def clear_all_banned_words_endpoint():
    _require_owner()
    _clear_all_banned_words()
    return jsonify({"ok": True})


@app.post("/api/admin/banned-words")
def add_banned_word_endpoint():
    owner_email = _require_owner()
    body = request.get_json(silent=True) or {}
    _add_banned_word(body.get("word"), owner_email)
    return jsonify({"ok": True})


@app.post("/api/admin/banned-words/bulk")
def bulk_add_banned_words_endpoint():
    """
    改行/カンマ区切りのテキストをまとめて登録する(1個ずつ登録するのが面倒な場合用)。
    { "text": "単語1\\n単語2\\n単語3" } のような形で渡す。
    """
    owner_email = _require_owner()
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    words = [w.strip() for w in re.split(r"[\n,]+", text) if w.strip()]
    added = 0
    for w in words[:2000]:  # 一度に大量に投げられて固まらないよう上限を設けておく
        try:
            _add_banned_word(w, owner_email)
            added += 1
        except ApiError:
            continue  # 個々の単語が長すぎる等で弾かれても、他の単語の登録は続ける
    return jsonify({"ok": True, "added": added, "total_submitted": len(words)})


@app.post("/api/admin/banned-words/import-url")
def import_banned_words_from_url_endpoint():
    """
    改行区切りの単語一覧が置いてある外部URL(例: GitHub上で公開されている
    定番のNGワードリスト等)を指定して、まとめて取り込む。単語の中身自体は
    こちらでは一切選定・生成しない(完全に外部リソースをそのまま使うだけ)。
    """
    owner_email = _require_owner()
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ApiError(400, "有効なURLを指定してください")
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        raise ApiError(502, f"URLの取得に失敗しました: {e}")
    if resp.status_code >= 400:
        raise ApiError(502, f"URLの取得に失敗しました: HTTP {resp.status_code}")

    words = [w.strip() for w in re.split(r"[\n,]+", resp.text) if w.strip() and not w.strip().startswith("#")]
    added = 0
    for w in words[:5000]:
        try:
            _add_banned_word(w, owner_email)
            added += 1
        except ApiError:
            continue
    return jsonify({"ok": True, "added": added, "total_found": len(words)})


@app.delete("/api/admin/banned-words/<int:word_id>")
def remove_banned_word_endpoint(word_id):
    _require_owner()
    _remove_banned_word(word_id)
    return jsonify({"ok": True})


@app.get("/api/admin/banned-ips")
def list_banned_ips_endpoint():
    _require_owner()
    return jsonify({"ips": _get_all_banned_ips()})


@app.post("/api/admin/banned-ips")
def add_banned_ip_endpoint():
    owner_email = _require_owner()
    body = request.get_json(silent=True) or {}
    ip = (body.get("ip") or "").strip()
    if not ip:
        raise ApiError(400, "ip is required")
    _ban_ip(ip, body.get("reason") or "手動でのBAN", banned_by=owner_email, is_manual=True)
    return jsonify({"ok": True})


@app.delete("/api/admin/banned-ips/<path:ip>")
def remove_banned_ip_endpoint(ip):
    owner_email = _require_owner()
    _unban_ip(ip, unbanned_by=owner_email)
    return jsonify({"ok": True})


@app.get("/api/admin/banned-emails")
def list_banned_emails_endpoint():
    _require_owner()
    return jsonify({"emails": _get_all_banned_emails()})


@app.post("/api/admin/banned-emails")
def add_banned_email_endpoint():
    owner_email = _require_owner()
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        raise ApiError(400, "email is required")
    _ban_email(email, body.get("reason") or "手動でのBAN", banned_by=owner_email, is_manual=True)
    return jsonify({"ok": True})


@app.delete("/api/admin/banned-emails/<path:email>")
def remove_banned_email_endpoint(email):
    owner_email = _require_owner()
    _unban_email(email, unbanned_by=owner_email)
    return jsonify({"ok": True})


@app.get("/api/admin/ban-events")
def list_ban_events_endpoint():
    """
    誤BANの可能性を確認するための、ユーザー別に絞り込めるBANログ。
    ?ip=... または ?email=... で絞り込める(どちらも省略時は全体の最新分)。
    """
    _require_owner()
    ip = request.args.get("ip")
    email = request.args.get("email")
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    return jsonify({"events": _get_ban_events(ip=ip, email=email, limit=limit)})


@app.get("/api/admin/moderation-policy")
def get_moderation_policy_endpoint():
    _require_owner()
    return jsonify({
        "policy": _get_moderation_policy(),
        "ai_enabled": bool(GROQ_API_KEY),
        "model": GROQ_MODEL,
    })


@app.put("/api/admin/moderation-policy")
def update_moderation_policy_endpoint():
    _require_owner()
    body = request.get_json(silent=True) or {}
    _set_moderation_policy(body.get("policy"))
    return jsonify({"ok": True})


@app.post("/api/moderation/check-search")
def check_search_moderation_endpoint():
    """
    検索キーワードにNGワードが含まれているかだけを確認する(検索実行前のチェック用)。
    ここではBANまではしない。フロントエンドが検索前に呼び出す想定。
    """
    body = request.get_json(silent=True) or {}
    query = body.get("query") or ""
    matched = _find_matched_banned_word(query)
    return jsonify({"blocked": matched is not None, "matched_word": matched if matched else None})


@app.get("/api/ban/check")
def check_my_ban_status_endpoint():
    """
    自分(リクエスト元IP)が現在BANされているかどうかを確認する。BAN済みの人自身も
    呼べる必要があるので、_BAN_EXEMPT_API_PATHSに入れて除外している。
    """
    client_ip = _get_client_ip()
    ban = _is_ip_banned(client_ip)
    return jsonify({"banned": ban is not None, "reason": ban["reason"] if ban else None})


@app.get("/api/user/subscriptions")
def get_user_subscriptions_endpoint():
    email = _authed_email()
    return jsonify({"subscriptions": _get_user_subscriptions(email)})


@app.post("/api/user/subscriptions")
def add_user_subscription_endpoint():
    email = _authed_email()
    body = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id")
    if not channel_id:
        raise ApiError(400, "channel_id is required")
    _add_user_subscription(email, channel_id, body.get("channel"), body.get("thumbnail"))
    return jsonify({"ok": True})


@app.delete("/api/user/subscriptions/<channel_id>")
def remove_user_subscription_endpoint(channel_id):
    email = _authed_email()
    _remove_user_subscription(email, channel_id)
    return jsonify({"ok": True})


@app.get("/api/user/likes")
def get_user_likes_endpoint():
    email = _authed_email()
    return jsonify({"likes": _get_user_likes(email)})


@app.post("/api/user/likes")
def add_user_like_endpoint():
    email = _authed_email()
    body = request.get_json(silent=True) or {}
    video_id = body.get("video_id")
    if not video_id:
        raise ApiError(400, "video_id is required")
    _add_user_like(email, video_id, body.get("title"), body.get("thumbnail"), body.get("channel"), body.get("duration"))
    return jsonify({"ok": True})


@app.delete("/api/user/likes/<video_id>")
def remove_user_like_endpoint(video_id):
    email = _authed_email()
    _remove_user_like(email, video_id)
    return jsonify({"ok": True})


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

  <div class="card">
    <div class="desc">
      このページを含む /api/* 全体は有効なトークンが無いと404になります
      (表向きのツールサイトの「トークン発行」から取得してください)。
      下の欄に貼っておけば、このページのテストボタンにも自動で使われます
      (このブラウザだけに保存されます)。
    </div>
    <div class="params">
      <label>APIトークン
        <input type="text" id="apiTokenInput" placeholder="トークン発行ツールで取得した文字列">
      </label>
    </div>
    <button class="run" id="apiTokenSaveBtn">保存</button>
  </div>

  <div id="cards"></div>

<script>
const API_TOKEN_STORAGE_KEY = "ytdlp_api_docs_token";

function currentApiToken() {
  const fromUrl = new URLSearchParams(window.location.search).get("token");
  if (fromUrl) {
    localStorage.setItem(API_TOKEN_STORAGE_KEY, fromUrl);
    return fromUrl;
  }
  return localStorage.getItem(API_TOKEN_STORAGE_KEY) || "";
}

document.getElementById("apiTokenInput").value = currentApiToken();
document.getElementById("apiTokenSaveBtn").addEventListener("click", () => {
  localStorage.setItem(API_TOKEN_STORAGE_KEY, document.getElementById("apiTokenInput").value.trim());
});

</script>
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
      const res = await fetch(url, { method: ep.method, headers: { "X-API-Token": localStorage.getItem(API_TOKEN_STORAGE_KEY) || "" } });
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


@app.get("/")
def landing_page():
    return render_template("landing.html")



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



@app.get("/api/suggest")
def suggest():
    """
    検索窓の入力補完(サジェスト)。YouTubeの検索ボックスと同じ公開サジェストAPIを使う。
    認証不要で軽量。client=firefox を指定すると、JSONPでラップされずに
    ["検索語", ["候補1", "候補2", ...]] という素のJSON配列で返ってくるので、
    JSONPの中身を取り出すパース処理が不要になり壊れにくい。
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"query": q, "suggestions": []})

    url = (
        "https://suggestqueries.google.com/complete/search"
        f"?client=firefox&ds=yt&hl=ja&gl=JP&q={urllib.parse.quote(q)}"
    )
    try:
        resp = _fetch_page(url, timeout=10)
    except ApiError:
        return jsonify({"query": q, "suggestions": []})
    if resp.status_code >= 400:
        return jsonify({"query": q, "suggestions": []})

    suggestions = []
    try:
        data = json.loads(resp.text)
        suggestions = [s for s in (data[1] or []) if s]
    except (json.JSONDecodeError, IndexError, TypeError):
        suggestions = []
    return jsonify({"query": q, "suggestions": suggestions[:10]})


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

    matched_word = _find_matched_banned_word(q)
    if matched_word:
        client_ip = _get_client_ip()
        _ban_ip(client_ip, "NGワードを含む検索", matched_word=matched_word, context=q[:200])
        token = request.headers.get("X-Session-Token", "")
        email = _verify_session_token(token) if token else None
        if email:
            _ban_email(email, "NGワードを含む検索", matched_word=matched_word, context=q[:200], ip=client_ip)
        log("denied", f"NGワード検索によりBAN: ip={client_ip} email={email} (word={matched_word})", "red")
        raise ApiError(403, "利用を制限されました。お問い合わせフォームからご連絡ください。")

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
        with _urlopen(req, timeout=timeout) as resp:
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


def _find_first_image_sources(node):
    """
    lockupViewModelのサムネイル置き場所は、YouTube側の実装変更で何度も
    パスが変わっている(collectionThumbnailViewModel.primaryThumbnailが
    無くなったり戻ったりしている)。既知のパスが全部外れた時の最終手段として、
    contentImage以下を再帰的に探索し、"url"キーを持つ配列(sourcesらしきもの)を
    見つけたらそれを使う。
    """
    if isinstance(node, dict):
        sources = node.get("sources")
        if isinstance(sources, list) and sources and isinstance(sources[0], dict) and "url" in sources[0]:
            return sources
        for value in node.values():
            found = _find_first_image_sources(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_first_image_sources(item)
            if found:
                return found
    return None


def _parse_lockup_view_model(node):
    """
    YouTubeの新しいUI形式(lockupViewModel)を解析する。
    videoRenderer等の従来形式とは構造がまるで違う(videoIdはcontentId、
    titleはmetadata.lockupMetadataViewModel.title.content、という具合に
    ネストが深く名前も違う)ため、_looks_like_videoの単純な総当たりでは拾えない。
    このノード形式専用の解析経路を別途用意している。

    lockupViewModelは動画だけでなく再生リスト(contentType が
    LOCKUP_CONTENT_TYPE_PLAYLIST)も同じ形で運んでくる。再生リストの場合、
    contentIdは動画IDではなく再生リストIDになる。またサムネイルの置き場所も
    contentImage.collectionThumbnailViewModel.primaryThumbnail.thumbnailViewModel...
    という入れ子になっていることがある(YouTube側の実装がこの数ヶ月で
    何度か変わっているため、両方の経路を試す)。
    """
    lockup = node.get("lockupViewModel")
    if not isinstance(lockup, dict):
        return None

    content_id = lockup.get("contentId")
    if not content_id:
        return None

    content_type = lockup.get("contentType") or ""
    is_playlist = content_type in ("LOCKUP_CONTENT_TYPE_PLAYLIST", "LOCKUP_CONTENT_TYPE_PODCAST")

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
        _dig(lockup, "contentImage", "collectionThumbnailViewModel", "primaryThumbnail", "thumbnailViewModel", "image", "sources")
        or _dig(lockup, "contentImage", "thumbnailViewModel", "image", "sources")
        or _dig(lockup, "contentImage", "image", "sources")
        or _find_first_image_sources(lockup.get("contentImage"))
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
        _dig(lockup, "contentImage", "collectionThumbnailViewModel", "primaryThumbnail", "thumbnailViewModel", "overlays")
        or _dig(lockup, "contentImage", "thumbnailViewModel", "overlays")
        or _dig(lockup, "contentImage", "overlays")
        or []
    )
    length_text = None
    video_count_text = None
    for overlay in overlays:
        badge_vm = _dig(overlay, "thumbnailOverlayBadgeViewModel", "thumbnailBadges", 0, "thumbnailBadgeViewModel")
        if badge_vm and badge_vm.get("text"):
            if is_playlist:
                video_count_text = badge_vm["text"]
            else:
                length_text = badge_vm["text"]
            break

    video_count = None
    if is_playlist and video_count_text:
        digits = "".join(ch for ch in video_count_text if ch.isdigit())
        if digits:
            video_count = int(digits)

    return {
        "video_id": content_id,
        "title": title,
        "channel": channel_name,
        "channel_id": channel_id,
        "channel_thumbnail": channel_avatar,
        "length_text": length_text,
        "view_count_text": views_text,
        "thumbnail": thumbnail_url,
        "entry_type": "playlist" if is_playlist else "video",
        "video_count": video_count,
        "url": (
            f"https://www.youtube.com/playlist?list={content_id}" if is_playlist
            else f"https://www.youtube.com/watch?v={content_id}"
        ),
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

        # 旧形式(playlistRenderer等)の再生リストカード。lockupViewModelに全面移行して
        # いない画面(検索結果・関連動画の一部)では、今でもこちらの形式で出てくることがある。
        # 従来は videoId が無いので _looks_like_video に弾かれて丸ごと無視されており、
        # これが「再生リストのサムネイルが出ない」不具合の原因になっていた
        # (正確には、サムネイルどころかカード自体が出ていなかった)。
        playlist_node = None
        for key in ("playlistRenderer", "compactPlaylistRenderer", "gridPlaylistRenderer", "radioRenderer"):
            if key in node:
                playlist_node = node[key]
                break
        if playlist_node is not None:
            playlist_id = playlist_node.get("playlistId")
            if not playlist_id or playlist_id == exclude_id or playlist_id in seen:
                continue
            title = _runs_text(playlist_node.get("title"))
            if not title:
                continue
            seen.add(playlist_id)

            thumb_sources = (
                _dig(playlist_node, "thumbnails", 0, "thumbnails")
                or _dig(playlist_node, "thumbnailRenderer", "playlistVideoThumbnailRenderer", "thumbnail", "thumbnails")
                or _dig(playlist_node, "thumbnail", "thumbnails")
                or _find_first_image_sources(playlist_node)
                or []
            )
            thumbnail_url = thumb_sources[-1]["url"] if thumb_sources else None

            video_count_text = (
                _dig(playlist_node, "videoCountShortText", "simpleText")
                or _dig(playlist_node, "videoCountText", "simpleText")
                or _runs_text(playlist_node.get("videoCountText"))
            )
            video_count = None
            if video_count_text:
                digits = "".join(ch for ch in video_count_text if ch.isdigit())
                if digits:
                    video_count = int(digits)

            entries.append({
                "video_id": playlist_id,
                "title": title,
                "channel": _runs_text(playlist_node.get("longBylineText") or playlist_node.get("shortBylineText")),
                "channel_id": None,
                "channel_thumbnail": None,
                "length_text": None,
                "view_count_text": None,
                "thumbnail": thumbnail_url,
                "entry_type": "playlist",
                "video_count": video_count,
                "url": f"https://www.youtube.com/playlist?list={playlist_id}",
            })
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
if PROXY_URL:
    _http.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
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

    以前は「このAPI経由で実際に視聴された動画」の集計だったが、まだ誰も見ていない
    うちは空っぽになってしまう問題があったため、外部の1時間おき更新のトレンド情報
    (https://github.com/siawaseok3/wakame の trend.json)を使う方式に切り替えた。
    ?category= で trending(総合) / music(音楽) / gaming(ゲーム) を切り替えられる。
    """
    limit = max(1, min(int(request.args.get("limit", 24)), 100))
    category = request.args.get("category", "trending")
    if category not in ("trending", "music", "gaming"):
        category = "trending"

    with _track_processing("trending", "trending"):
        data = _fetch_wakame_trending()

    entries = (data.get("categories") or {}).get(category, [])[:limit]

    return jsonify(_json_safe({
        "method": "external_feed",
        "source": "https://github.com/siawaseok3/wakame",
        "category": category,
        "updated": data.get("updated"),
        "note": (
            "外部の1時間おき更新のトレンド情報を使用しています"
            "(YouTube公式のトレンドページそのものではありません)。"
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
        return jsonify(cached)

    with _track_processing(video_id, "info"):
        data = _extract_full(video_id)
        _check_video_moderation_or_ban(video_id, data)

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


def _build_stream_payload(video_id, data, include_info=True, channel_avatar_url=None, channel_avatar_base64=None):
    """
    再生に必要なストリーム情報に加えて、動画の詳細情報(info相当)も一緒に返す。
    以前はフロントエンドが /api/info と /api/stream を毎回両方叩いていて、
    yt-dlpでの重い抽出(Node.jsでの署名解読含む)が動画1本につき2回走っていた。
    1回のレスポンスにまとめることで、無駄なく1回の抽出で済ませられる
    (include_info=Falseにすれば、以前と同じ軽量なstreamだけの形式で返せる)。
    """
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

    result = {
        "video_id": data.get("id") or video_id,
        "title": data.get("title"),
        "is_live": data.get("is_live", False),
        "streams": streams,
        "hls_url": hls_url,
        "proxy_url_template": f"/api/proxy-stream/{video_id}?format_id={{format_id}}",
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    }
    if include_info:
        info_payload = _build_info_payload(data)
        info_payload["channel_avatar"] = channel_avatar_url
        info_payload["channel_avatar_base64"] = channel_avatar_base64
        # ストリーム系のキー(streams/hls_url等)を優先しつつ、infoの内容もマージする
        merged = dict(info_payload)
        merged.update(result)
        result = merged

    return _json_safe(result)


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


@app.get("/api/stream-fast/<video_id>")
def stream_fast(video_id):
    """
    android_vrクライアントだけを使った高速版。動画ページを開いた直後、
    通常の(遅い)/api/streamの結果を待たずにすぐ再生を始めたい場合に使う。
    低画質(360p相当)のフォーマットしか返せない・キャッシュもしない
    (あくまで「とりあえず今すぐ再生を始める」ための一時的なデータのため)。
    取得できなかった場合は、そのことが分かるように ok:false を返す
    (エラーにはしない。呼び出し側で通常の/api/streamにフォールバックする想定)。
    """
    data = _extract_fast(video_id)
    if not data:
        return jsonify({"ok": False})

    # 高速版でもモデレーションは省略しない
    _check_video_moderation_or_ban(video_id, data)

    result = _build_stream_payload(video_id, data)
    result["ok"] = True
    result["fast"] = True
    return jsonify(result)


@app.get("/api/stream/<video_id>")
def stream(video_id):
    key = f"stream:{video_id}"
    cached = _response_cache_get(key)
    if cached is not None:
        return jsonify(cached)

    with _track_processing(video_id, "stream"):
        data = _extract_full(video_id)
        _check_video_moderation_or_ban(video_id, data)

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
        channel_avatar_base64 = _image_to_data_uri(channel_avatar_url) if want_base64 else None

    result = _build_stream_payload(
        video_id, data,
        channel_avatar_url=channel_avatar_url,
        channel_avatar_base64=channel_avatar_base64,
    )
    if not result.get("streams") and not result.get("hls_url"):
        # ignore_no_formats_error で例外は抑えたが、結局1つも再生可能なデータが
        # 得られなかった場合(ライブ配信がまだ始まっていない、等)は、
        # 空っぽの再生不可能なレスポンスをそのまま返さず、分かりやすいエラーにする。
        raise ApiError(400, "再生可能な動画/配信データが見つかりませんでした(配信がまだ始まっていない可能性があります)")
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
