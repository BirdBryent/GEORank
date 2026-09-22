"""公司删除时的外部资源清理。

删除接口只负责数据库行，向量库 / 知识图谱 / 对象存储 / AI 额度预占都必须显式清理，
否则会留下孤儿数据：已删公司仍能被语义检索命中、Neo4j 节点永久残留、
MinIO 原始快照无人回收、AI 预占长期占用额度。

外部依赖失败不阻断删除（数据库行已提交），逐项记录结果并由调用方返回。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging_utils import log_event

logger = logging.getLogger(__name__)


def company_storage_keys(company: Any) -> list[str]:
    """收集公司占用的对象存储 key：原始快照、附件页、截图。"""
    keys: list[str] = []

    def _add(key: Any) -> None:
        if isinstance(key, str) and key.strip():
            normalized = key.strip()
            if normalized not in keys:
                keys.append(normalized)

    _add(getattr(company, "raw_html_key", None))
    _add(getattr(company, "about_html_key", None))

    for page in getattr(company, "crawl_pages", None) or []:
        if isinstance(page, dict):
            _add(page.get("key"))

    screenshots = getattr(company, "screenshots", None) or []
    if isinstance(screenshots, dict):
        screenshots = screenshots.get("items") or []
    for shot in screenshots if isinstance(screenshots, list) else []:
        if isinstance(shot, dict):
            _add(shot.get("key") or shot.get("storage_key"))
        else:
            _add(shot)

    return keys


async def cleanup_company_external_resources(
    *,
    company_id: str,
    storage_keys: Iterable[str],
    db: AsyncSession,
    reservation_id: Any = None,
) -> dict[str, Any]:
    """清理被删公司的外部资源，返回逐项结果。每项独立兜底，不抛出异常。"""
    result: dict[str, Any] = {
        "vectors": "skipped",
        "graph_nodes": 0,
        "storage_removed": 0,
        "storage_remaining": 0,
        "reservation": "skipped",
    }

    try:
        from app.services.vector_store import vector_store

        if vector_store.delete_company_vectors(company_id):
            result["vectors"] = "deleted"
        else:
            # 集合还不存在 = 本来就没有向量残留，不是失败
            result["vectors"] = "skipped:no_collection"
    except Exception as exc:  # 向量库不可用不应阻断公司删除
        result["vectors"] = f"failed:{type(exc).__name__}"
        log_event(
            logger,
            logging.WARNING,
            "company.cleanup.vectors_failed",
            company_id=company_id,
            error=str(exc)[:300],
        )

    try:
        from app.services.graph_store import delete_company_graph

        result["graph_nodes"] = await delete_company_graph(company_id)
    except Exception as exc:
        result["graph_nodes"] = f"failed:{type(exc).__name__}"
        log_event(
            logger,
            logging.WARNING,
            "company.cleanup.graph_failed",
            company_id=company_id,
            error=str(exc)[:300],
        )

    try:
        from app.services.storage import storage

        for key in storage_keys:
            storage.delete(key)
            # storage.delete 静默失败，回读一次确认对象确实消失
            if storage.get(key) is None:
                result["storage_removed"] += 1
            else:
                result["storage_remaining"] += 1
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "company.cleanup.storage_failed",
            company_id=company_id,
            error=str(exc)[:300],
        )

    if reservation_id:
        try:
            from app.services.ai_usage import settle_token_reservation

            # 已结算/已释放的预占会被 settle 直接跳过，重复调用安全。
            await settle_token_reservation(
                db,
                reservation_id=reservation_id,
                actual_tokens=0,
                succeeded=False,
            )
            await db.commit()
            result["reservation"] = "settled_failed"
        except Exception as exc:
            await db.rollback()
            result["reservation"] = f"failed:{type(exc).__name__}"
            log_event(
                logger,
                logging.WARNING,
                "company.cleanup.reservation_failed",
                company_id=company_id,
                error=str(exc)[:300],
            )

    log_event(logger, logging.INFO, "company.cleanup.completed", company_id=company_id, **result)
    return result
