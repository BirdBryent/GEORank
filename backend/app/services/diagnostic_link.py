"""
诊断报告 ↔ 公司 的自动关联（按域名）

背景：诊断报告只在创建时**显式传入 company_id** 才会关联公司（前台从公司详情页发起才带）。
用户直接粘贴 URL 发起的诊断，company_id 永远是 null —— 即使对应公司后来审核通过了也不会回头补，
后台报告列表就一直显示「未关联公司」。

本模块把未关联的报告按域名（host 归一化）回填到**已发布**的公司上。
注意不能用 `normalize_company_url()` 直接比字符串：它不合并 www.、不统一 scheme、不剥路径，
`https://www.flyai66.top` 与 `https://flyai66.top` 会被判成不同，漏匹配。
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse
from typing import Any

from sqlalchemy import select

from app.models.company import Company, PublishStatus
from app.models.diagnostic import DiagnosticReport

logger = logging.getLogger(__name__)


def normalize_host(url: str | None) -> str:
    """把 URL 归一成可比较的 host：小写、去端口、去 `www.` 前缀。

    非法或空 URL 返回空串（调用方据此跳过）。
    """
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = f"https://{text.lstrip('/')}"
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


async def link_unlinked_reports(db) -> dict[str, Any]:
    """把 `company_id` 为空的诊断报告，按域名关联到已发布公司。

    同域名有多家已发布公司时取**最新创建**的那家（url 有唯一约束，但 www/非 www 可能是两行）。
    返回 `{"scanned", "linked", "skipped": [...], "linked_items": [...]}`。
    """
    reports = (
        await db.execute(
            select(DiagnosticReport)
            .where(DiagnosticReport.company_id.is_(None))
            .order_by(DiagnosticReport.created_at)
        )
    ).scalars().all()

    if not reports:
        return {"scanned": 0, "linked": 0, "skipped": [], "linked_items": []}

    companies = (
        await db.execute(
            select(Company)
            .where(Company.publish_status == PublishStatus.PUBLISHED)
            .order_by(Company.created_at)
        )
    ).scalars().all()

    by_host: dict[str, Company] = {}
    for company in companies:
        host = normalize_host(company.url)
        if host:
            by_host[host] = company  # 后创建的覆盖先创建的 → 取最新

    linked_items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for report in reports:
        host = normalize_host(report.url)
        company = by_host.get(host) if host else None
        if company is None:
            skipped.append(
                {
                    "report_id": str(report.id),
                    "url": report.url,
                    "reason": "no_published_company",
                }
            )
            continue
        report.company_id = company.id
        linked_items.append(
            {
                "report_id": str(report.id),
                "url": report.url,
                "company_id": str(company.id),
                "company_name": company.name,
            }
        )

    if linked_items:
        await db.commit()
        logger.info(
            "diagnostic reports linked to companies: %s of %s",
            len(linked_items),
            len(reports),
        )

    return {
        "scanned": len(reports),
        "linked": len(linked_items),
        "linked_items": linked_items,
        "skipped": skipped,
    }
