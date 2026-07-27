"""
ytdlp_api - yt-dlp を使ったシンプルな動画情報/ストリーム一覧/HLS配信API
UIなし(/api/statsのみUIあり)、API専用。Termux + ngrok での運用を想定。

Flask + requests のみで構成(fastapi/pydanticは不使用。Rustビルド不要)。

エンドポイント:
  GET    /api                                  API一覧・説明・実行テスト用ページ(HTML)
  GET    /api/info/{video_id}                 動画の全メタデータ(ストリームURLは含まない)
  GET    /api/stream/{video_id}                その動画の全ストリームURL一覧 + HLS(m3u8)リンク
  GET    /api/hls/{video_id}                   リアルタイムHLS変換した再生リスト(m3u8)を返す。無ければ自動で変換開始
  GET    /api/hls/{video_id}/{filename}        m3u8 / tsセグメントの配信(HLSプレイヤーが内部的に叩く)
  POST   /api/hls/{video_id}/stop              変換ジョブ停止・一時ファイル削除
  GET    /api/health                           死活監視
  GET    /api/stats                            worker/処理中/キャッシュ/稼働時間を見るダッシュボード(HTML)
  GET    /api/stats/data                       ↑と同じ内容をJSONで返す(ポーリング用)
  GET    /api/workers                          このサーバー(worker)の情報一覧
  GET    /api/processing                       現在処理中のvideo_id一覧(経過時間つき)
  GET    /api/cache                            これまでに解決した動画の一覧(Video ID / Title)
  GET    /api/cache/{video_id}                 キャッシュ済みの単一動画の情報
  DELETE /api/cache/{video_id}                 キャッシュから削除

video_id には
  - YouTubeの動画ID (例: dQw4w9WgXcQ)
  - もしくはURLエンコードした完全なURL (例: https%3A%2F%2Fvimeo.com%2F12345)
のどちらも指定できます。単純な文字列(URLでない)場合は自動的に
https://www.youtube.com/watch?v={video_id} として扱われます。

レスポンスキャッシュについて:
  /api/info と /api/stream は、同じvideo_idに対する結果を7時間(YTDLP_API_CACHE_TTL_SECONDS)
  保存し、期間内の再リクエストはyt-dlpを呼ばずに即座にキャッシュを返します。
  レスポンスの "_cache" フィールドで hit/miss と残り有効時間が確認できます。
  ※ CDN側の直リンク(googlevideo等)は数時間で失効することがあるため、
     再生に失敗する場合はキャッシュ有効期間内でも一度削除して取り直してください
     (DELETE /api/cache/{video_id} は「一覧用インデックス」のみを消すため、
      レスポンスキャッシュ自体は自然失効を待つか、サーバー再起動で消えます)。
"""

import os
import re
import json
import time
import shutil
import sqlite3
import threading
import subprocess
import urllib.parse
import contextlib
from datetime import datetime

import requests
import yt_dlp
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)

TMP_DIR_PATH = os.environ.get("YTDLP_API_TMP", os.path.join(os.path.expanduser("~"), "ytdlp_api_tmp"))
os.makedirs(TMP_DIR_PATH, exist_ok=True)

HLS_JOBS = {}  # job_id -> subprocess.Popen
JOB_TTL_SEC = int(os.environ.get("YTDLP_API_JOB_TTL", "1800"))  # 30分操作が無ければ自動失効

# hls変換用に選んだ直リンクの短期キャッシュ (video_id, format_id) -> (url, expire_at, data)
_HLS_URL_CACHE = {}
HLS_URL_CACHE_TTL_SEC = int(os.environ.get("YTDLP_API_URLCACHE_TTL", "300"))  # 5分

# /api/info, /api/stream の結果を保存する期間
RESPONSE_CACHE_TTL_SECONDS = int(os.environ.get("YTDLP_API_CACHE_TTL_SECONDS", str(7 * 3600)))  # 7時間

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
    """info取得・stream解決・hls変換開始などの間、処理中一覧に載せておく。"""
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


def _sanitize_id(video_id):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", video_id)[:64]


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "nocheckcertificate": True,
    }
    if extra:
        opts.update(extra)
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


def _resolve_direct_url(video_id, format_id, use_cache=True):
    """hls変換(ffmpeg入力)用に、単一フォーマットのCDN直リンクを取得する。"""
    cache_key = (video_id, format_id)
    if use_cache and cache_key in _HLS_URL_CACHE:
        cached_url, expire_at, data = _HLS_URL_CACHE[cache_key]
        if time.time() < expire_at:
            return cached_url, data

    source_url = _resolve_url(video_id)
    data = _extract(source_url, {"format": format_id})
    _cache_upsert(video_id, data)
    stream_url = data.get("url")
    if not stream_url and data.get("requested_formats"):
        stream_url = data["requested_formats"][0].get("url")
    if not stream_url:
        raise ApiError(404, "direct url not found for this format")

    _HLS_URL_CACHE[cache_key] = (stream_url, time.time() + HLS_URL_CACHE_TTL_SEC, data)
    return stream_url, data


# ---------- health ----------

@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "jobs": len(HLS_JOBS), "uptime_seconds": _uptime_seconds()})


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
  { method: "GET", path: "/api/info/{video_id}", desc: "動画の全メタデータを取得(ストリームURLは含まない)。7時間キャッシュ。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
  { method: "GET", path: "/api/stream/{video_id}", desc: "その動画の全ストリームURL一覧 + HLS(m3u8)リンクを取得。7時間キャッシュ。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
  { method: "GET", path: "/api/hls/{video_id}", desc: "リアルタイムHLS変換の再生リスト(m3u8)を返す。未変換なら自動で開始。",
    params: [
      { name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" },
      { name: "format_id", in: "query", placeholder: "best" },
    ] },
  { method: "POST", path: "/api/hls/{video_id}/stop", desc: "HLS変換ジョブを停止し、一時ファイルを削除する。",
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
  { method: "DELETE", path: "/api/cache/{video_id}", desc: "一覧インデックスから削除する(レスポンスキャッシュは別途自然失効)。",
    params: [{ name: "video_id", in: "path", placeholder: "dQw4w9WgXcQ" }] },
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
    with _CACHE_DB_LOCK, _db() as conn:
        cur = conn.execute("DELETE FROM cache WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM response_cache WHERE video_id = ?", (video_id,))
    if cur.rowcount == 0:
        raise ApiError(404, "not in cache")
    return jsonify({"deleted": video_id})


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
        return jsonify(cached)

    with _track_processing(video_id, "info"):
        data = _extract_full(video_id)

    result = _build_info_payload(data)
    _response_cache_set(key, "info", video_id, result)

    result = dict(result)
    result["_cache"] = {
        "hit": False,
        "age_seconds": 0,
        "expires_in_seconds": RESPONSE_CACHE_TTL_SECONDS,
    }
    return jsonify(result)


# ---------- stream (全ストリームURL一覧 + HLSリンク) ----------

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
        "local_hls_endpoint": f"/api/hls/{video_id}",  # 自前のリアルタイム変換(VODでも常に使える)
        "cache_ttl_seconds": RESPONSE_CACHE_TTL_SECONDS,
    })


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


# ---------- hls (リアルタイム変換) ----------

def _cleanup_stale_jobs():
    now = time.time()
    for job_id, proc in list(HLS_JOBS.items()):
        job_dir = os.path.join(TMP_DIR_PATH, job_id)
        marker = os.path.join(job_dir, ".started")
        if os.path.exists(marker) and now - os.path.getmtime(marker) > JOB_TTL_SEC:
            proc.terminate()
            HLS_JOBS.pop(job_id, None)
            shutil.rmtree(job_dir, ignore_errors=True)


def _watch_hls_job(video_id, proc):
    """ffmpegの終了を待って処理中一覧から外すバックグラウンドスレッド。"""
    proc.wait()
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS.pop(video_id, None)


def _start_hls_job(video_id, format_id):
    job_id = _sanitize_id(video_id)

    existing = HLS_JOBS.get(job_id)
    if existing and existing.poll() is None:
        open(os.path.join(TMP_DIR_PATH, job_id, ".started"), "a").close()  # TTLを延長
        return job_id

    with _track_processing(video_id, "hls-resolve"):
        stream_url, _ = _resolve_direct_url(video_id, format_id, use_cache=False)

    job_dir = os.path.join(TMP_DIR_PATH, job_id)
    os.makedirs(job_dir, exist_ok=True)
    open(os.path.join(job_dir, ".started"), "a").close()

    cmd = [
        "ffmpeg", "-y",
        "-i", stream_url,
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "0",
        "-hls_segment_filename", os.path.join(job_dir, "seg_%03d.ts"),
        os.path.join(job_dir, "index.m3u8"),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    HLS_JOBS[job_id] = proc

    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS[video_id] = {
            "worker": SERVER_NAME,
            "type": "hls-transcode",
            "started_at": time.time(),
        }
    threading.Thread(target=_watch_hls_job, args=(video_id, proc), daemon=True).start()

    return job_id


@app.get("/api/hls/<video_id>")
def hls_playlist(video_id):
    format_id = request.args.get("format_id", "best")
    _cleanup_stale_jobs()
    job_id = _start_hls_job(video_id, format_id)

    playlist_path = os.path.join(TMP_DIR_PATH, job_id, "index.m3u8")
    for _ in range(50):  # 最大5秒待機
        if os.path.exists(playlist_path):
            break
        time.sleep(0.1)

    if not os.path.exists(playlist_path):
        raise ApiError(503, "transcoding not ready yet, retry shortly")

    return send_file(playlist_path, mimetype="application/vnd.apple.mpegurl")


@app.get("/api/hls/<video_id>/<filename>")
def hls_file(video_id, filename):
    if "/" in filename or ".." in filename:
        raise ApiError(400, "invalid filename")
    job_id = _sanitize_id(video_id)
    path = os.path.join(TMP_DIR_PATH, job_id, filename)
    if not os.path.exists(path):
        raise ApiError(404, "not found (still transcoding? wait a moment)")
    media_type = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return send_file(path, mimetype=media_type)


@app.post("/api/hls/<video_id>/stop")
def hls_stop(video_id):
    job_id = _sanitize_id(video_id)
    proc = HLS_JOBS.pop(job_id, None)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    shutil.rmtree(os.path.join(TMP_DIR_PATH, job_id), ignore_errors=True)
    return jsonify({"stopped": job_id})


if __name__ == "__main__":
    port = int(os.environ.get("YTDLP_API_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
