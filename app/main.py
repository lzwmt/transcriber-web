"""HTTP 接口。不做鉴权，只给本机或内网用。"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import config, recording, service
from app.jobs import JobStore
from app.pipeline import job_directory
from app.redact import redact_sensitive, strip_urls
from app.web import router as web_router
from app.worker import WorkerSupervisor

logger = logging.getLogger(__name__)

_RESTART_MESSAGE = "服务重启，任务已中断"

# 下载端点只许取常量集合
ALLOWED_DOWNLOAD_FILENAMES = frozenset(
    {"transcript.md", "segments.json", "minutes.md", "combined.md"}
)


def sanitize_download_filename(filename: str) -> str:
    """清洗下载文件名，移除换行符、回车符及路径分隔符，防御 CRLF 注入与路径穿越。"""
    cleaned = filename.replace("\r", "").replace("\n", "").strip()
    return Path(cleaned).name


def create_app(data: Path | None = None) -> FastAPI:
    root = (data if data is not None else config.data_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = JobStore(root / "jobs.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.mark_running_as_error(_RESTART_MESSAGE)
        # 启动时回收卡死的 assembling 会话（I6）：进程在拼合中途被杀会留下不可见卡死。
        if config.recording_enabled():
            try:
                recording.reclaim_stale_sessions(store, root)
            except Exception:  # 清理失败不该挡住服务启动
                logger.exception("reclaim stale recording sessions failed")
        supervisor = WorkerSupervisor(root, store)
        supervisor.start()
        app.state.supervisor = supervisor
        try:
            yield
        finally:
            supervisor.stop()
    app = FastAPI(lifespan=lifespan)
    app.state.store = store
    app.state.data_dir = root
    app.include_router(web_router)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/jobs")
    async def create_job(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            return await _create_from_json(request, store, root)
        if "multipart/form-data" in content_type:
            return await _create_from_upload(request, store, root)
        return JSONResponse(
            status_code=415,
            content={"error": "需要 JSON 或 multipart 请求"},
        )

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> JSONResponse:
        body = service.public_job(store, job_id)
        if body is None:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        return JSONResponse(content=body)

    @app.get("/jobs/{job_id}/transcript", response_model=None)
    def get_transcript(job_id: str) -> FileResponse | JSONResponse:
        return _download(store, root, job_id, "transcript.md", "text/markdown; charset=utf-8")

    @app.get("/jobs/{job_id}/segments", response_model=None)
    def get_segments(job_id: str) -> FileResponse | JSONResponse:
        return _download(store, root, job_id, "segments.json", "application/json; charset=utf-8")

    @app.get("/jobs/{job_id}/audio", response_model=None)
    def get_audio(job_id: str, fmt: str | None = None) -> FileResponse | JSONResponse:
        return _serve_audio(store, root, job_id, fmt=fmt)

    @app.get("/jobs/{job_id}/minutes", response_model=None)
    def get_minutes(job_id: str) -> FileResponse | JSONResponse:
        status, payload = service.minutes_payload(store, root, job_id)
        if status != 200:
            assert isinstance(payload, dict)
            return JSONResponse(status_code=status, content=payload)
        assert isinstance(payload, Path)
        return FileResponse(
            payload, media_type="text/markdown; charset=utf-8", filename="minutes.md"
        )

    @app.get("/jobs/{job_id}/combined", response_model=None)
    def get_combined(job_id: str) -> Response:
        status, payload = service.combined_payload(store, root, job_id)
        if status != 200:
            assert isinstance(payload, dict)
            return JSONResponse(status_code=status, content=payload)
        assert isinstance(payload, str)
        return Response(
            content=payload.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="combined.md"'},
        )

    @app.post("/jobs/{job_id}/minutes/regenerate", response_model=None)
    def regenerate_minutes(request: Request, job_id: str) -> JSONResponse | RedirectResponse:
        status, body = service.regenerate_minutes(store, job_id)
        if _wants_html(request):
            if status == 404:
                return JSONResponse(status_code=404, content=body)
            return RedirectResponse(f"/jobs/{job_id}/view", status_code=303)
        return JSONResponse(status_code=status, content=body)

    @app.delete("/jobs/{job_id}")
    def delete_job(job_id: str) -> JSONResponse:
        if not service.delete_job(store, root, job_id):
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        return JSONResponse(content={"ok": True})

    @app.post("/jobs/{job_id}/delete", response_model=None)
    def delete_job_form(request: Request, job_id: str) -> JSONResponse | RedirectResponse:
        if not service.delete_job(store, root, job_id):
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        return JSONResponse(content={"ok": True})

    @app.post("/jobs/{job_id}/rename", response_model=None)
    async def rename_job(request: Request, job_id: str) -> JSONResponse:
        name = await _read_name_field(request)
        if name is None:
            return JSONResponse(status_code=400, content={"error": "缺少 name"})
        status, body = service.rename_job(store, job_id, name)
        return JSONResponse(status_code=status, content=body)

    # --- 在线录音（LZW-62）：开关关时统一 404，不暴露路由存在 ---------------
    def _recording_closed() -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "录音功能未开放"})

    @app.post("/recordings")
    async def create_recording(request: Request) -> JSONResponse:
        if not config.recording_enabled():
            return _recording_closed()
        payload = await _read_json_object(request)
        if payload is None:
            return JSONResponse(status_code=400, content={"error": "JSON 无法解析"})
        try:
            tier = _validate_tier(payload.get("tier") or "fast")
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        name = payload.get("name")
        source_name = name.strip() if isinstance(name, str) and name.strip() else None
        try:
            body = await run_in_threadpool(
                recording.create_session, store, root, tier=tier, source_name=source_name
            )
        except recording.RecordingError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        return JSONResponse(content=body)

    @app.post("/recordings/{session_id}/parts/{index}")
    async def upload_recording_part(session_id: str, index: int, request: Request) -> JSONResponse:
        if not config.recording_enabled():
            return _recording_closed()
        try:
            upload = await run_in_threadpool(
                recording.begin_part, store, root, session_id, index
            )
        except recording.RecordingError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        # 流式收片（I7）：边读边写临时文件，超限立即中断并删掉残留，不把整块进内存。
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(upload.write, chunk)
            body = await run_in_threadpool(upload.finish)
        except recording.RecordingError as exc:
            await run_in_threadpool(upload.abort)
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        except Exception:
            await run_in_threadpool(upload.abort)
            raise
        return JSONResponse(content=body)

    @app.post("/recordings/{session_id}/finish")
    async def finish_recording(session_id: str, request: Request) -> JSONResponse:
        if not config.recording_enabled():
            return _recording_closed()
        payload = await _read_json_object(request, allow_empty=True)
        if payload is None:
            return JSONResponse(status_code=400, content={"error": "JSON 无法解析"})
        try:
            tier = _validate_tier(payload.get("tier") or "fast")
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        name = payload.get("name")
        source_name = name.strip() if isinstance(name, str) and name.strip() else None
        try:
            expected_parts = recording.parse_expected_parts(payload.get("expected_parts"))
        except recording.RecordingError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        try:
            body = await run_in_threadpool(
                recording.finish_session,
                store,
                root,
                session_id,
                tier=tier,
                source_name=source_name,
                expected_parts=expected_parts,
            )
        except recording.RecordingError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        return JSONResponse(content=body)

    @app.get("/recordings/{session_id}")
    def recording_status(session_id: str) -> JSONResponse:
        if not config.recording_enabled():
            return _recording_closed()
        try:
            body = recording.session_status(store, root, session_id)
        except recording.RecordingError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        return JSONResponse(content=body)

    @app.delete("/recordings/{session_id}")
    def delete_recording(session_id: str) -> JSONResponse:
        if not config.recording_enabled():
            return _recording_closed()
        if not recording.delete_session(store, root, session_id):
            return JSONResponse(status_code=404, content={"error": "录音会话不存在"})
        return JSONResponse(content={"ok": True})

    return app


async def _read_json_object(
    request: Request, *, allow_empty: bool = False
) -> dict[str, Any] | None:
    """读 JSON 对象；空 body 且 allow_empty 时按 {} 处理。解析失败回 None。"""
    raw = await request.body()
    if not raw.strip():
        return {} if allow_empty else None
    try:
        payload: Any = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


async def _create_from_json(request: Request, store: JobStore, root: Path) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON 无法解析"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "JSON 必须是对象"})
    if payload.get("file"):
        return JSONResponse(status_code=400, content={"error": "JSON 提交只接受 url"})
    try:
        tier = _validate_tier(payload.get("tier", "fast"))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        return JSONResponse(status_code=400, content={"error": "缺少 url"})
    status, body = await run_in_threadpool(
        service.create_url_job, store, root, url.strip(), tier
    )
    return JSONResponse(status_code=status, content=body)


async def _create_from_upload(request: Request, store: JobStore, root: Path) -> JSONResponse:
    form = await request.form()
    upload = form.get("file")
    url = form.get("url")
    if upload is not None and isinstance(url, str) and url.strip():
        return JSONResponse(status_code=400, content={"error": "文件和链接只能提交一种"})
    if not isinstance(upload, UploadFile):
        return JSONResponse(status_code=400, content={"error": "缺少上传文件"})
    try:
        tier = _validate_tier(form.get("tier") or "fast")
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    status, body = await service.create_upload_job(store, root, upload, tier)
    return JSONResponse(status_code=status, content=body)


def _validate_tier(value: object) -> str:
    return service.validate_tier(value)


async def _read_name_field(request: Request) -> object | None:
    """改名入参兼容 JSON {"name": ...} 与表单 name=...；取不到字段回 None。"""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload: Any = await request.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload.get("name")
    try:
        form = await request.form()
    except Exception:
        return None
    value = form.get("name")
    return value if isinstance(value, str) else None


def _wants_html(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _download(
    store: JobStore,
    root: Path,
    job_id: str,
    filename: str,
    media_type: str,
) -> FileResponse | JSONResponse:
    if not service.is_job_id(job_id):
        return JSONResponse(status_code=404, content={"error": "任务不存在"})
    if filename not in ALLOWED_DOWNLOAD_FILENAMES:
        return JSONResponse(status_code=404, content={"error": "结果暂不可用"})
    try:
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        if job["status"] != "done":
            return JSONResponse(
                status_code=404,
                content={"error": "任务尚未完成，结果不可下载", "status": job["status"]},
            )
        job_dir = job_directory(root, job_id).resolve()
        path = (job_dir / filename).resolve()
        if not path.is_relative_to(job_dir) or not path.is_file():
            return JSONResponse(
                status_code=404,
                content={"error": "结果文件不存在", "status": job["status"]},
            )
        return FileResponse(path, media_type=media_type, filename=filename)
    except Exception as exc:
        logger.error(
            "download failed: %s: %s",
            type(exc).__name__,
            strip_urls(redact_sensitive(str(exc))),
        )
        return JSONResponse(status_code=404, content={"error": "结果暂不可用"})


def _serve_audio(
    store: JobStore,
    root: Path,
    job_id: str,
    fmt: str | None = None,
) -> FileResponse | JSONResponse:
    if not service.is_job_id(job_id):
        return JSONResponse(status_code=404, content={"error": "任务不存在"})
    try:
        job = store.get(job_id)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        if job["status"] != "done":
            return JSONResponse(
                status_code=409,
                content={"error": "任务尚未完成，音频不可用", "status": job["status"]},
            )
        jobs_root = (root / "jobs").resolve()
        job_dir = job_directory(root, job_id).resolve()
        if not job_dir.is_relative_to(jobs_root) or not job_dir.is_dir():
            return JSONResponse(
                status_code=404,
                content={"error": "任务不存在"},
            )
        target_file: Path | None
        if fmt == "wav":
            target_file = job_dir / "audio.wav"
        elif fmt is not None:
            return JSONResponse(
                status_code=404,
                content={"error": "音频文件不存在", "status": job["status"]},
            )
        else:
            target_file = service.resolve_job_audio(job_dir)
        if target_file is None:
            return JSONResponse(
                status_code=404,
                content={"error": "音频文件不存在", "status": job["status"]},
            )
        path = target_file.resolve()
        if not path.is_relative_to(job_dir) or not path.is_file():
            return JSONResponse(
                status_code=404,
                content={"error": "音频文件不存在", "status": job["status"]},
            )
        ext = path.suffix.lower().lstrip(".")
        media_type = service.AUDIO_MEDIA_TYPES.get(ext, "audio/wav")
        return FileResponse(path, media_type=media_type)
    except Exception as exc:
        logger.error(
            "serve audio failed: %s: %s",
            type(exc).__name__,
            strip_urls(redact_sensitive(str(exc))),
        )
        return JSONResponse(status_code=404, content={"error": "音频暂不可用"})


