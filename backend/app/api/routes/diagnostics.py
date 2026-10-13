"""
GEO 诊断 API — 提交 URL → 爬取 → 分析 → 报告
"""
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from app.core.deps import DbSession, CurrentUser, OptionalUser
from app.core.timeutils import to_utc_iso
from app.models.diagnostic import DiagnosticReport, DiagnosticStatus
from app.services.company_ingest import normalize_company_url
from app.services.ai_usage import release_ai_access, resolve_async_ai_access
from app.schemas.diagnostic import (
    DiagnoseRequest, DiagnoseResponse, DiagnosticReportResponse, DiagnosticHistoryItem,
)

router = APIRouter()
_QUEUE_DISPATCH_ERROR = "诊断任务创建失败，请稍后重试。"


@router.post("/", response_model=DiagnoseResponse)
async def start_diagnosis(data: DiagnoseRequest, request: Request, db: DbSession, current_user: OptionalUser):
    """
    开始 GEO 诊断：
    1. 创建 DiagnosticReport 记录 (pending)
    2. 触发 Celery diagnose 任务
    3. 返回 report_id，前端轮询状态
    """
    try:
        normalized_url = normalize_company_url(data.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    company_id = uuid.UUID(data.company_id) if data.company_id else None
    access = await resolve_async_ai_access(
        db=db,
        current_user=current_user,
        module="diagnostics",
        prompt_text=f"{normalized_url}\n{data.company_id or ''}",
        request=request,
    )

    report = DiagnosticReport(
        url=normalized_url,
        company_id=company_id,
        status=DiagnosticStatus.PENDING,
        user_id=current_user.id if current_user else None,
        ai_reservation_id=access.reservation_id,
    )
    db.add(report)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await release_ai_access(db, access, error_code="diagnostic_record_create_failed")
        await db.commit()
        raise
    await db.refresh(report)

    # 触发 Celery 任务：先爬取页面，再进入 analyze_page
    queue_failed = False
    try:
        from app.core.celery_app import celery_app
        celery_app.send_task(
            "app.tasks.crawl.crawl_diagnostic_page",
            args=[str(report.id), normalized_url, str(access.reservation_id)],
        )
    except Exception:
        queue_failed = True
        report.status = DiagnosticStatus.FAILED
        report.error_message = _QUEUE_DISPATCH_ERROR
        await release_ai_access(db, access, error_code="queue_dispatch_failed")
        await db.commit()

    return DiagnoseResponse(
        report_id=str(report.id),
        status="failed" if queue_failed else "pending",
    )


@router.get("/history")
async def diagnosis_history(db: DbSession, current_user: CurrentUser, page: int = 1, size: int = 20):
    """当前用户的诊断历史"""
    result = await db.execute(
        select(DiagnosticReport)
        .where(DiagnosticReport.user_id == current_user.id)
        .order_by(DiagnosticReport.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    reports = result.scalars().all()
    return [
        {
            "report_id": str(r.id),
            "url": r.url,
            "status": r.status.value,
            "overall_score": r.overall_score,
            "created_at": to_utc_iso(r.created_at),
        }
        for r in reports
    ]


@router.get("/{report_id}")
async def get_report(report_id: str, db: DbSession, current_user: OptionalUser):
    """获取诊断报告（前端轮询直到 completed）"""
    result = await db.execute(
        select(DiagnosticReport).where(DiagnosticReport.id == uuid.UUID(report_id))
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if report.user_id is not None and (not current_user or report.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="无权访问此报告")

    return {
        "report_id": str(report.id),
        "url": report.url,
        "company_id": str(report.company_id) if report.company_id else None,
        "status": report.status.value,
        "overall_score": report.overall_score,
        "schema_analysis": report.schema_analysis,
        "content_analysis": report.content_analysis,
        "meta_analysis": report.meta_analysis,
        "citation_analysis": report.citation_analysis,
        "recommendations": report.recommendations,
        "error_message": report.error_message,
        "created_at": to_utc_iso(report.created_at),
    }


# ===== 报告 PDF（前台导出，与后台同一套渲染链路） =====

async def _load_visible_report(report_id: str, db: DbSession, current_user) -> DiagnosticReport:
    """按 get_report 的可见性规则取报告：非本人报告 403。"""
    try:
        rid = uuid.UUID(report_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="报告不存在")
    report = (
        await db.execute(select(DiagnosticReport).where(DiagnosticReport.id == rid))
    ).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if report.user_id is not None and (not current_user or report.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="无权访问此报告")
    return report


async def _report_pdf_payload(report: DiagnosticReport, db: DbSession) -> dict:
    """组装 PDF 渲染所需的 payload（字段与后台导出保持同构）。"""
    from app.models.company import Company
    from app.services.runtime_settings import get_diagnostic_rule_config

    company = None
    if report.company_id:
        company = (
            await db.execute(select(Company).where(Company.id == report.company_id))
        ).scalar_one_or_none()
    rules = await get_diagnostic_rule_config()
    return {
        "id": str(report.id),
        "url": report.url,
        "status": report.status.value,
        "overall_score": report.overall_score,
        "company_id": str(report.company_id) if report.company_id else None,
        "company_name": company.name if company else None,
        "schema_analysis": report.schema_analysis or {},
        "content_analysis": report.content_analysis or {},
        "meta_analysis": report.meta_analysis or {},
        "citation_analysis": report.citation_analysis or {},
        "recommendations": report.recommendations or {},
        "error_message": report.error_message,
        "created_at": to_utc_iso(report.created_at),
        "rule_config": rules,
    }


def _report_pdf_filename(report_id: str) -> str:
    return f"diagnostic-report-{report_id}.pdf"


@router.post("/{report_id}/export-pdf")
async def create_report_pdf(report_id: str, db: DbSession, current_user: OptionalUser):
    """前台触发 PDF 报告生成（渲染在 crawler 容器执行，前端轮询后下载）。"""
    report = await _load_visible_report(report_id, db, current_user)
    if report.status != DiagnosticStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="报告尚未完成，暂不能导出 PDF")

    payload = await _report_pdf_payload(report, db)
    from app.core.celery_app import celery_app

    try:
        async_result = celery_app.send_task(
            "app.tasks.report.render_diagnostic_report_pdf",
            args=[report_id, payload],
            queue="report",
        )
    except Exception:
        raise HTTPException(status_code=503, detail="任务队列不可用，PDF 生成请求未受理")

    return {
        "task_id": async_result.id,
        "state": "PENDING",
        "status": "pending",
        "filename": _report_pdf_filename(report_id),
    }


@router.get("/{report_id}/export-pdf/{task_id}")
async def get_report_pdf_task(
    report_id: str,
    task_id: str,
    db: DbSession,
    current_user: OptionalUser,
):
    """查询 PDF 生成任务状态。"""
    await _load_visible_report(report_id, db, current_user)
    from app.core.celery_app import celery_app

    result = celery_app.AsyncResult(task_id)
    state = result.state
    payload = {
        "task_id": task_id,
        "state": state,
        "filename": _report_pdf_filename(report_id),
    }
    if state == "SUCCESS":
        info = result.result if isinstance(result.result, dict) else {}
        payload.update({"status": "ready", "size": info.get("size")})
    elif state == "FAILURE":
        payload.update({"status": "failed", "detail": str(result.result)[:300]})
    else:
        payload.update({"status": "pending"})
    return payload


@router.get("/{report_id}/export-pdf/{task_id}/download")
async def download_report_pdf(
    report_id: str,
    task_id: str,
    db: DbSession,
    current_user: OptionalUser,
):
    """下载已生成的 PDF 报告。"""
    await _load_visible_report(report_id, db, current_user)
    from app.core.celery_app import celery_app
    from app.services.storage import storage

    result = celery_app.AsyncResult(task_id)
    if result.state != "SUCCESS":
        raise HTTPException(status_code=409, detail="PDF 尚未生成完成")

    info = result.result if isinstance(result.result, dict) else {}
    key = info.get("key")
    data = storage.get(key) if key else None
    if not data:
        raise HTTPException(status_code=404, detail="PDF 文件不存在或已被清理")

    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_report_pdf_filename(report_id)}"'
        },
    )
