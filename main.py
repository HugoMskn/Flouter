import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logging.disable(logging.CRITICAL)
for _name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "asyncio"):
    _logger = logging.getLogger(_name)
    _logger.disabled = True
    _logger.handlers = []
    _logger.propagate = False

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
DEFACE_TIMEOUT_SECONDS = int(os.environ.get("DEFACE_TIMEOUT_SECONDS", "900"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "1800"))
CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# tqdm's default bar format always contains "<int>%|" regardless of the
# description text, so this never needs to capture deface's own log line.
_PROGRESS_RE = re.compile(rb"(\d{1,3})%\|")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

JOBS: dict[str, dict] = {}


async def _sweep_stale_jobs() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for job_id, job in list(JOBS.items()):
            if now - job["created"] > JOB_TTL_SECONDS:
                job["output_path"].unlink(missing_ok=True)
                JOBS.pop(job_id, None)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    sweeper = asyncio.create_task(_sweep_stale_jobs())
    yield
    sweeper.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.exception_handler(Exception)
async def _opaque_error(_request, _exc):
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    return {"status": "ok"}


def _validate_extension(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="unsupported file type")
    return suffix


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    total = 0
    async with aiofiles.open(destination, "wb") as out:
        while chunk := await upload.read(CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="file too large")
            await out.write(chunk)
    await upload.close()


async def _monitor_progress(stream: asyncio.StreamReader, job: dict) -> None:
    buf = b""
    while True:
        chunk = await stream.read(256)
        if not chunk:
            return
        buf += chunk
        while True:
            cut = min((i for i in (buf.find(b"\r"), buf.find(b"\n")) if i != -1), default=-1)
            if cut == -1:
                break
            line, buf = buf[:cut], buf[cut + 1:]
            match = _PROGRESS_RE.search(line)
            if match:
                job["progress"] = max(job["progress"], min(99, int(match.group(1))))


async def _process_job(job_id: str, input_path: Path, output_path: Path) -> None:
    job = JOBS[job_id]
    proc = None
    monitor_task = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "deface",
            str(input_path),
            "-o",
            str(output_path),
            "--keep-audio",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        monitor_task = asyncio.create_task(_monitor_progress(proc.stderr, job))
        returncode = await asyncio.wait_for(proc.wait(), timeout=DEFACE_TIMEOUT_SECONDS)
        if returncode != 0 or not output_path.exists():
            raise RuntimeError("deface failed")
        job["progress"] = 100
        job["status"] = "done"
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        job["status"] = "error"
        output_path.unlink(missing_ok=True)
    except Exception:
        job["status"] = "error"
        output_path.unlink(missing_ok=True)
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
        input_path.unlink(missing_ok=True)


async def _stream_and_wipe(path: Path):
    try:
        async with aiofiles.open(path, "rb") as f:
            while chunk := await f.read(CHUNK_SIZE):
                yield chunk
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/process")
async def process_video(file: UploadFile = File(...)):
    suffix = _validate_extension(file.filename)
    job_id = uuid.uuid4().hex
    input_path = DATA_DIR / f"{job_id}-in{suffix}"
    output_path = DATA_DIR / f"{job_id}-out{suffix}"

    try:
        await _save_upload(file, input_path)
    except HTTPException:
        input_path.unlink(missing_ok=True)
        raise

    job = {
        "status": "processing",
        "progress": 0,
        "output_path": output_path,
        "created": time.monotonic(),
        "task": None,
    }
    JOBS[job_id] = job
    job["task"] = asyncio.create_task(_process_job(job_id, input_path, output_path))

    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return {"status": job["status"], "progress": job["progress"]}


@app.get("/api/result/{job_id}")
async def result(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")

    if job["status"] == "processing":
        raise HTTPException(status_code=409, detail="not ready")

    if job["status"] == "error":
        JOBS.pop(job_id, None)
        raise HTTPException(status_code=422, detail="processing failed")

    output_path = job["output_path"]
    size = output_path.stat().st_size
    JOBS.pop(job_id, None)

    return StreamingResponse(
        _stream_and_wipe(output_path),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="blurred.mp4"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
    )
