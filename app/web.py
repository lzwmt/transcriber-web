"""网页路由。数据只经 service 层，这里不写 SQL。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import config, recording, service
from app.jobs import JobStore

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _store(request: Request) -> JobStore:
    store = request.app.state.store
    assert isinstance(store, JobStore)
    return store


def _root(request: Request) -> Path:
    root = request.app.state.data_dir
    assert isinstance(root, Path)
    return root


def _workbench(request: Request, error: str | None = None) -> dict[str, Any]:
    store = _store(request)
    root = _root(request)
    listed = service.list_jobs_for_page(store)
    now = datetime.now(UTC)
    usage = service.monthly_usage(store, now)
    return {
        "jobs": listed["jobs"],
        "job_total": listed["total"],
        "usage": usage["text"],
        "banner": service.recent_failure_banner(store, now),
        "has_unfinished": service.job_has_unfinished(store),
        "page": "list",
        "error": error,
        "recording_enabled": config.recording_enabled(),
        # 未完成的录音会话与 job 并列渲染；开关关时不查、不显示（D0b）。
        "recording_sessions": (
            recording.session_cards(store, root) if config.recording_enabled() else []
        ),
    }


@router.get("/", response_class=HTMLResponse)
def list_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "list.html", _workbench(request))


@router.get("/new", response_model=None)
def redirect_new() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=302)


def _submitted_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


@router.post("/new", response_model=None)
async def submit_new(request: Request) -> HTMLResponse | RedirectResponse:
    store = _store(request)
    root = _root(request)
    form = await request.form()
    submitted_url = _submitted_text(form.get("url"))
    submitted_tier = _submitted_text(form.get("tier")) or "fast"
    try:
        tier = service.validate_tier(form.get("tier") or "fast")
    except ValueError as exc:
        return _new_error(
            request,
            str(exc),
            submitted_url=submitted_url,
            submitted_tier=submitted_tier,
        )
    upload = form.get("file")
    has_file = isinstance(upload, UploadFile) and bool(upload.filename)
    has_url = bool(submitted_url)
    if has_file and has_url:
        return _new_error(
            request,
            "文件和链接只能提交一种",
            submitted_url=submitted_url,
            submitted_tier=tier,
        )
    if not has_file and not has_url:
        return _new_error(
            request,
            "请上传文件或粘贴链接",
            submitted_url=submitted_url,
            submitted_tier=tier,
        )
    if has_url:
        status, body = await run_in_threadpool(
            service.create_url_job, store, root, submitted_url, tier
        )
    else:
        assert isinstance(upload, UploadFile)
        status, body = await service.create_upload_job(store, root, upload, tier)
    if status != 200:
        return _new_error(
            request,
            str(body.get("error") or "提交失败"),
            status,
            submitted_url=submitted_url,
            submitted_tier=tier,
        )
    return RedirectResponse(f"/jobs/{body['job_id']}/view", status_code=303)


@router.get("/jobs/{job_id}/view", response_class=HTMLResponse)
def detail_page(request: Request, job_id: str) -> HTMLResponse:
    job = service.job_detail_for_page(_store(request), _root(request), job_id)
    if job is None:
        return TEMPLATES.TemplateResponse(
            request,
            "missing.html",
            {"page": "missing"},
            status_code=404,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "detail.html",
        {
            "job": job,
            "refresh": bool(job["unfinished"]),
            "page": "detail",
        },
    )



@router.get("/record", response_class=HTMLResponse)
def record_page(request: Request) -> HTMLResponse:
    """录音页。开关关时统一 404（协调者 §7-④ 裁定）。"""
    if not config.recording_enabled():
        return _recording_missing(request)
    store = _store(request)
    return TEMPLATES.TemplateResponse(
        request,
        "record.html",
        {
            "page": "record",
            "prefill_name": store.peek_unnamed_name(),
            "limits": recording.limits_payload(),
        },
    )


@router.get("/recordings/{session_id}/view", response_class=HTMLResponse)
def recording_detail_page(request: Request, session_id: str) -> HTMLResponse:
    """待补传详情页：只显示已保存片数与已录时长，无播放器、无下载入口。"""
    if not config.recording_enabled():
        return _recording_missing(request)
    if not recording.is_session_id(session_id):
        return _recording_missing(request)
    store = _store(request)
    session = store.get_recording_session(session_id)
    if session is None:
        return _recording_missing(request)
    detail = recording.session_status(store, _root(request), session_id)
    return TEMPLATES.TemplateResponse(
        request,
        "recording_detail.html",
        {"page": "recording_detail", "session": detail},
    )


def _recording_missing(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "missing.html",
        {"page": "missing"},
        status_code=404,
    )

def _new_error(
    request: Request,
    message: str,
    status_code: int = 400,
    *,
    submitted_url: str = "",
    submitted_tier: str = "fast",
) -> HTMLResponse:
    context = _workbench(request, message)
    context["submitted_url"] = submitted_url
    context["submitted_tier"] = submitted_tier
    return TEMPLATES.TemplateResponse(
        request,
        "list.html",
        context,
        status_code=status_code,
    )
