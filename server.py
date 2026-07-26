"""
ytdlp_api - yt-dlp を使ったシンプルな動画情報/ストリーム/HLS配信API
UIなし、API専用。Termux + ngrok での運用を想定。

エンドポイント:
  GET  /api/info/{video_id}                  動画情報 + フォーマット一覧
  GET  /api/stream/{video_id}                 サーバー経由でのプロキシ再生(Range/シーク対応)
  GET  /api/hls/{video_id}                    リアルタイムHLS変換した再生リスト(m3u8)を返す。無ければ自動で変換開始
  GET  /api/hls/{video_id}/{filename}         m3u8 / tsセグメントの配信(HLSプレイヤーが内部的に叩く)
  POST /api/hls/{video_id}/stop               変換ジョブ停止・一時ファイル削除
  GET  /api/health                            死活監視
  GET  /api/stats                             worker数・処理中件数・キャッシュ件数・稼働時間をまとめて返す
  GET  /api/workers                           このサーバー(worker)の情報一覧
  GET  /api/processing                        現在処理中のvideo_id一覧(経過時間つき)
  GET  /api/cache                             これまでに解決した動画のキャッシュ一覧(Video ID / Title)
  GET  /api/cache/{video_id}                  キャッシュ済みの単一動画の情報
  DELETE /api/cache/{video_id}                 キャッシュから削除

video_id には
  - YouTubeの動画ID (例: dQw4w9WgXcQ)
  - もしくはURLエンコードした完全なURL (例: https%3A%2F%2Fvimeo.com%2F12345)
のどちらも指定できます。単純な文字列(URLでない)場合は自動的に
https://www.youtube.com/watch?v={video_id} として扱われます。

共通クエリパラメータ:
  format_id (省略可、デフォルト "best")  yt-dlpのフォーマット指定と同じ書式
"""

import os
import re
import time
import shutil
import sqlite3
import threading
import subprocess
import urllib.parse
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse

app = FastAPI(title="ytdlp-api", description="yt-dlp powered video API (no UI)")

TMP_DIR = Path(os.environ.get("YTDLP_API_TMP", str(Path.home() / "ytdlp_api_tmp")))
TMP_DIR.mkdir(parents=True, exist_ok=True)

HLS_JOBS: dict[str, subprocess.Popen] = {}
JOB_TTL_SEC = int(os.environ.get("YTDLP_API_JOB_TTL", "1800"))  # 30分操作が無ければ自動失効

# 解決済み直リンクURLの短期キャッシュ (video_id, format_id) -> (url, expire_at, data)
_URL_CACHE: dict[tuple, tuple] = {}
URL_CACHE_TTL_SEC = int(os.environ.get("YTDLP_API_URLCACHE_TTL", "300"))  # 5分

# ---------- worker / 稼働状況 ----------

SERVER_ID = os.environ.get("YTDLP_API_SERVER_ID", "1")
SERVER_NAME = os.environ.get("YTDLP_API_SERVER_NAME", f"Server {SERVER_ID}")
SERVER_ROLE = os.environ.get("YTDLP_API_ROLE", "primary")  # 複数台構成なら secondary 等を指定
START_TIME = time.time()

# 現在処理中のジョブ: video_id -> {"worker":..., "type":..., "started_at": epoch秒}
_ACTIVE_JOBS: dict[str, dict] = {}
_ACTIVE_JOBS_LOCK = threading.Lock()


@contextlib.contextmanager
def _track_processing(video_id: str, job_type: str):
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


def _uptime_seconds() -> float:
    return round(time.time() - START_TIME, 1)


# ---------- 永続キャッシュ (SQLite) ----------

CACHE_DB_PATH = TMP_DIR / "cache.db"
_CACHE_DB_LOCK = threading.Lock()


def _cache_db() -> sqlite3.Connection:
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


def _cache_upsert(video_id: str, data: dict):
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


def _cache_count() -> int:
    with _CACHE_DB_LOCK, _cache_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM cache").fetchone()
        return row["c"] if row else 0


# ---------- 共通ヘルパー ----------

def _resolve_url(video_id: str) -> str:
    """video_idがURLならデコードしてそのまま使い、そうでなければYouTube動画とみなす。"""
    decoded = urllib.parse.unquote(video_id)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", decoded):
        return decoded
    return f"https://www.youtube.com/watch?v={decoded}"


def _sanitize_id(video_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", video_id)[:64]


def _ydl_opts(extra: Optional[dict] = None) -> dict:
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


def _extract(source_url: str, format_id: str = "best") -> dict:
    try:
        with yt_dlp.YoutubeDL(_ydl_opts({"format": format_id})) as ydl:
            return ydl.extract_info(source_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"yt-dlp error: {e}")


def _extract_cached(video_id: str, format_id: str = "best") -> dict:
    """yt-dlpでの解決結果を取得しつつ、永続キャッシュ(SQLite)にも書き込む。"""
    source_url = _resolve_url(video_id)
    data = _extract(source_url, format_id)
    _cache_upsert(video_id, data)
    return data


def _get_direct_url(video_id: str, format_id: str, use_cache: bool = True):
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
        raise HTTPException(status_code=404, detail="direct url not found for this format")

    _URL_CACHE[cache_key] = (stream_url, time.time() + URL_CACHE_TTL_SEC, data)
    return stream_url, data


# ---------- health ----------

@app.get("/api/health")
def health():
    return {"status": "ok", "jobs": len(HLS_JOBS), "uptime_seconds": _uptime_seconds()}


# ---------- workers / 処理中 / stats ----------

def _workers_snapshot() -> list[dict]:
    with _ACTIVE_JOBS_LOCK:
        processing_count = len(_ACTIVE_JOBS)
    return [{
        "server_id": SERVER_ID,
        "worker": SERVER_NAME,
        "processing": processing_count,
        "role": SERVER_ROLE,
    }]


def _processing_snapshot() -> list[dict]:
    now = time.time()
    with _ACTIVE_JOBS_LOCK:
        items = list(_ACTIVE_JOBS.items())
    result = []
    for video_id, job in items:
        started_dt = datetime.fromtimestamp(job["started_at"], tz=timezone.utc).astimezone()
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
    return _workers_snapshot()


@app.get("/api/processing")
def processing():
    return _processing_snapshot()


@app.get("/api/stats")
def stats():
    """ダッシュボード用にworker・処理中・キャッシュ件数・稼働時間をまとめて返す。"""
    return {
        "workers": _workers_snapshot(),
        "processing": _processing_snapshot(),
        "cache_count": _cache_count(),
        "uptime_seconds": _uptime_seconds(),
    }


# ---------- cache (これまでに解決した動画の永続キャッシュ) ----------

@app.get("/api/cache")
def cache_list(limit: int = 50, offset: int = 0, q: Optional[str] = None):
    limit = max(1, min(limit, 500))
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

    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/cache/{video_id}")
def cache_get(video_id: str):
    with _CACHE_DB_LOCK, _cache_db() as conn:
        row = conn.execute("SELECT * FROM cache WHERE video_id = ?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not in cache")
    return {
        "video_id": row["video_id"],
        "title": row["title"],
        "thumbnail": row["thumbnail"],
        "duration": row["duration"],
        "uploader": row["uploader"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


@app.delete("/api/cache/{video_id}")
def cache_delete(video_id: str):
    with _CACHE_DB_LOCK, _cache_db() as conn:
        cur = conn.execute("DELETE FROM cache WHERE video_id = ?", (video_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="not in cache")
    return {"deleted": video_id}


# ---------- info ----------

@app.get("/api/info/{video_id}")
def info(video_id: str, format_id: str = "best"):
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
    return JSONResponse({
        "id": data.get("id"),
        "title": data.get("title"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "uploader": data.get("uploader"),
        "is_live": data.get("is_live", False),
        "formats": formats,
    })


# ---------- stream (プロキシ再生・Range対応) ----------

@app.get("/api/stream/{video_id}")
async def stream(video_id: str, request: Request, format_id: str = "best"):
    """
    CDN直リンクへプロキシしつつ、クライアントのRangeヘッダをそのまま転送する。
    シーク(早送り/巻き戻し)にはRangeリクエストが必須なので、ここで対応している。
    """
    with _track_processing(video_id, "stream"):
        stream_url, data = _get_direct_url(video_id, format_id)

    range_header = request.headers.get("range")
    fwd_headers = {}
    if range_header:
        fwd_headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    try:
        upstream_req = client.build_request("GET", stream_url, headers=fwd_headers)
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {e}")

    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream returned {upstream.status_code}")

    passthrough_headers = {}
    for h in ("content-range", "content-length", "accept-ranges", "content-type"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]
    passthrough_headers.setdefault("accept-ranges", "bytes")
    passthrough_headers.setdefault("content-type", data.get("ext") and f"video/{data['ext']}" or "video/mp4")

    async def gen():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    status_code = 206 if range_header and "content-range" in upstream.headers else upstream.status_code
    return StreamingResponse(gen(), status_code=status_code, headers=passthrough_headers)


# ---------- hls (リアルタイム変換) ----------

def _cleanup_stale_jobs():
    now = time.time()
    for job_id, proc in list(HLS_JOBS.items()):
        job_dir = TMP_DIR / job_id
        marker = job_dir / ".started"
        if marker.exists() and now - marker.stat().st_mtime > JOB_TTL_SEC:
            proc.terminate()
            HLS_JOBS.pop(job_id, None)
            shutil.rmtree(job_dir, ignore_errors=True)


def _watch_hls_job(video_id: str, proc: subprocess.Popen):
    """ffmpegの終了を待って処理中一覧から外すバックグラウンドスレッド。"""
    proc.wait()
    with _ACTIVE_JOBS_LOCK:
        _ACTIVE_JOBS.pop(video_id, None)


def _start_hls_job(video_id: str, format_id: str) -> str:
    job_id = _sanitize_id(video_id)

    # 既に変換中ならそのまま使い回す
    existing = HLS_JOBS.get(job_id)
    if existing and existing.poll() is None:
        (TMP_DIR / job_id / ".started").touch()  # TTLを延長
        return job_id

    with _track_processing(video_id, "hls-resolve"):
        stream_url, _ = _get_direct_url(video_id, format_id, use_cache=False)

    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / ".started").touch()

    cmd = [
        "ffmpeg", "-y",
        "-i", stream_url,
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "0",
        "-hls_segment_filename", str(job_dir / "seg_%03d.ts"),
        str(job_dir / "index.m3u8"),
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


@app.get("/api/hls/{video_id}")
def hls_playlist(video_id: str, format_id: str = "best"):
    """このvideo_id用のHLS変換ジョブが無ければ開始し、m3u8を返す。"""
    _cleanup_stale_jobs()
    job_id = _start_hls_job(video_id, format_id)

    # m3u8が生成されるまで少し待つ(ffmpeg起動直後は最初のセグメントができるまで存在しない)
    playlist_path = TMP_DIR / job_id / "index.m3u8"
    for _ in range(50):  # 最大5秒待機
        if playlist_path.exists():
            break
        time.sleep(0.1)

    if not playlist_path.exists():
        raise HTTPException(status_code=503, detail="transcoding not ready yet, retry shortly")

    return FileResponse(playlist_path, media_type="application/vnd.apple.mpegurl")


@app.get("/api/hls/{video_id}/{filename}")
def hls_file(video_id: str, filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    job_id = _sanitize_id(video_id)
    path = TMP_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found (still transcoding? wait a moment)")
    media_type = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path, media_type=media_type)


@app.post("/api/hls/{video_id}/stop")
def hls_stop(video_id: str):
    job_id = _sanitize_id(video_id)
    proc = HLS_JOBS.pop(job_id, None)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    shutil.rmtree(TMP_DIR / job_id, ignore_errors=True)
    return {"stopped": job_id}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("YTDLP_API_PORT", "5000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
