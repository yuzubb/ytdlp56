import os
import re
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
from flask import Flask, request, jsonify, Response, send_file, abort

app = Flask(__name__)

TMP_DIR_PATH = os.environ.get("YTDLP_API_TMP", os.path.join(os.path.expanduser("~"), "ytdlp_api_tmp"))
os.makedirs(TMP_DIR_PATH, exist_ok=True)

HLS_JOBS = {}  # job_id -> subprocess.Popen
JOB_TTL_SEC = int(os.environ.get("YTDLP_API_JOB_TTL", "1800"))  # 30分操作が無ければ自動失効

# 解決済み直リンクURLの短期キャッシュ (video_id, format_id) -> (url, expire_at, data)
_URL_CACHE = {}
URL_CACHE_TTL_SEC = int(os.environ.get("YTDLP_API_URLCACHE_TTL", "300"))  # 5分

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


# ---------- 永続キャッシュ (SQLite) ----------

CACHE_DB_PATH = os.path.join(TMP_DIR_PATH, "cache.db")
_CACHE_DB_LOCK = threading.Lock()


def _cache_db():
    conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_cache_db():
    with _CACHE_DB_LOCK, _cache_db() as conn:
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


_init_cache_db()


def _cache_upsert(video_id, data):
    now = time.time()
    with _CACHE_DB_LOCK, _cache_db() as conn:
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
    with _CACHE_DB_LOCK, _cache_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM cache").fetchone()
        return row["c"] if row else 0


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


def _extract(source_url, format_id="best"):
    try:
        with yt_dlp.YoutubeDL(_ydl_opts({"format": format_id})) as ydl:
            return ydl.extract_info(source_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ApiError(400, f"yt-dlp error: {e}")


def _extract_cached(video_id, format_id="best"):
    """yt-dlpでの解決結果を取得しつつ、永続キャッシュ(SQLite)にも書き込む。"""
    source_url = _resolve_url(video_id)
    data = _extract(source_url, format_id)
    _cache_upsert(video_id, data)
    return data


def _get_direct_url(video_id, format_id, use_cache=True):
    """CDN直リンクを取得する。短時間はキャッシュして毎回yt-dlpを叩かないようにする。"""
    cache_key = (video_id, format_id)
    if use_cache and cache_key in _URL_CACHE:
        cached_url, expire_at, data = _URL_CACHE[cache_key]
        if time.time() < expire_at:
            return cached_url, data

    data = _extract_cached(video_id, format_id)
    stream_url = data.get("url")
    if not stream_url and data.get("requested_formats"):
        stream_url = data["requested_formats"][0].get("url")
    if not stream_url:
        raise ApiError(404, "direct url not found for this format")

    _URL_CACHE[cache_key] = (stream_url, time.time() + URL_CACHE_TTL_SEC, data)
    return stream_url, data


# ---------- health ----------

@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "jobs": len(HLS_JOBS), "uptime_seconds": _uptime_seconds()})


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


@app.get("/api/workers")
def workers():
    return jsonify(_workers_snapshot())


@app.get("/api/processing")
def processing():
    return jsonify(_processing_snapshot())


@app.get("/api/stats")
def stats():
    """ダッシュボード用にworker・処理中・キャッシュ件数・稼働時間をまとめて返す。"""
    return jsonify({
        "workers": _workers_snapshot(),
        "processing": _processing_snapshot(),
        "cache_count": _cache_count(),
        "uptime_seconds": _uptime_seconds(),
    })


# ---------- cache (これまでに解決した動画の永続キャッシュ) ----------

@app.get("/api/cache")
def cache_list():
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    offset = max(0, int(request.args.get("offset", 0)))
    q = request.args.get("q")

    with _CACHE_DB_LOCK, _cache_db() as conn:
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
    with _CACHE_DB_LOCK, _cache_db() as conn:
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
    with _CACHE_DB_LOCK, _cache_db() as conn:
        cur = conn.execute("DELETE FROM cache WHERE video_id = ?", (video_id,))
    if cur.rowcount == 0:
        raise ApiError(404, "not in cache")
    return jsonify({"deleted": video_id})


# ---------- info ----------

@app.get("/api/info/<video_id>")
def info(video_id):
    format_id = request.args.get("format_id", "best")
    with _track_processing(video_id, "info"):
        data = _extract_cached(video_id, format_id)

    formats = []
    for f in data.get("formats", []) or []:
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "tbr": f.get("tbr"),
            "protocol": f.get("protocol"),
        })
    return jsonify({
        "id": data.get("id"),
        "title": data.get("title"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "uploader": data.get("uploader"),
        "is_live": data.get("is_live", False),
        "formats": formats,
    })


# ---------- stream (プロキシ再生・Range対応) ----------

@app.get("/api/stream/<video_id>")
def stream(video_id):
    """
    CDN直リンクへプロキシしつつ、クライアントのRangeヘッダをそのまま転送する。
    シーク(早送り/巻き戻し)にはRangeリクエストが必須なので、ここで対応している。
    """
    format_id = request.args.get("format_id", "best")

    with _track_processing(video_id, "stream"):
        stream_url, data = _get_direct_url(video_id, format_id)

    range_header = request.headers.get("Range")
    fwd_headers = {}
    if range_header:
        fwd_headers["Range"] = range_header

    try:
        upstream = requests.get(stream_url, headers=fwd_headers, stream=True, timeout=30)
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
    passthrough_headers.setdefault(
        "Content-Type", (data.get("ext") and f"video/{data['ext']}") or "video/mp4"
    )

    def gen():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    status_code = 206 if range_header and "Content-Range" in upstream.headers else upstream.status_code
    return Response(gen(), status=status_code, headers=passthrough_headers)


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

    # 既に変換中ならそのまま使い回す
    existing = HLS_JOBS.get(job_id)
    if existing and existing.poll() is None:
        open(os.path.join(TMP_DIR_PATH, job_id, ".started"), "a").close()  # TTLを延長
        return job_id

    with _track_processing(video_id, "hls-resolve"):
        stream_url, _ = _get_direct_url(video_id, format_id, use_cache=False)

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

    # ffmpegが動いている間は「hls変換中」として処理中一覧に出す
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
    """このvideo_id用のHLS変換ジョブが無ければ開始し、m3u8を返す。"""
    format_id = request.args.get("format_id", "best")
    _cleanup_stale_jobs()
    job_id = _start_hls_job(video_id, format_id)

    # m3u8が生成されるまで少し待つ(ffmpeg起動直後は最初のセグメントができるまで存在しない)
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
