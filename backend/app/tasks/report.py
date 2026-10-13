"""
诊断报告 PDF 渲染任务

队列说明：只有 crawler 容器（Playwright 镜像）里有 Chromium，
因此该任务固定路由到 report 队列，由 crawler worker 消费。
"""
from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.report.render_diagnostic_report_pdf", bind=True, max_retries=1)
def render_diagnostic_report_pdf(
    self,
    report_id: str,
    detail: dict[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """把报告 payload 渲染成 PDF 并写入对象存储，返回存储 key 与文件大小。"""
    from app.services.report_pdf import build_report_html, render_pdf
    from app.services.storage import storage

    html = build_report_html(detail, generated_at=generated_at)
    pdf_bytes = render_pdf(html, landscape=True)

    key = f"diagnostic-reports/{report_id}/{self.request.id}.pdf"
    stored = storage.put(key, pdf_bytes, content_type="application/pdf")
    if not stored:
        # 内存降级只存在于当前进程，API 容器读不到，必须显式失败
        raise RuntimeError("PDF 已生成，但写入对象存储失败，请检查 MinIO 状态后重试")

    logger.info("diagnostic report pdf rendered: %s (%s bytes)", key, len(pdf_bytes))
    return {
        "key": key,
        "size": len(pdf_bytes),
        "report_id": report_id,
        "filename": f"diagnostic-report-{report_id}.pdf",
    }
