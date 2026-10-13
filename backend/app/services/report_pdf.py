"""
诊断报告 PDF 生成 — 服务端渲染（Playwright / Chromium）

设计要点：
- 黑白印刷风格（无彩色），封面页 + 正文页，A4 横向
- 独立 HTML 模板，不复用前台站点 CSS，避免深色 UI 混进来
- 需要中文字体（crawler 镜像已安装 fonts-noto-cjk）
- 由 Celery 在 crawl 队列执行（只有 crawler 容器里有 Chromium）
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime
from typing import Any

# ===== 文案与结构常量 =====

BRAND_TEXT = "FlyAI 生成式引擎优化（GEO）"
REPORT_TITLE = "GEO 诊断报告"

DIMENSION_META = {
    "schema": ("结构化数据", "Schema"),
    "content": ("内容结构", "Content"),
    "meta": ("元信息", "Meta"),
    "citation": ("引用与信任", "Citation"),
}


def _esc(value: Any) -> str:
    return html_lib.escape(str(value if value is not None else ""), quote=True)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_score(value: Any) -> str:
    return f"{_num(value):.0f}"


def _normalize_weights(rule_config: dict | None) -> dict[str, float]:
    """把后台权重归一化成百分比。兼容 0.3 与 30 两种写法。"""
    raw = (rule_config or {}).get("weights") or rule_config or {}
    weights = {
        key: max(0.0, _num((raw or {}).get(key), 0.0))
        for key in ("schema", "content", "meta", "citation")
    }
    total = sum(weights.values())
    if total <= 0:
        return {"schema": 30.0, "content": 30.0, "meta": 20.0, "citation": 20.0}
    if total <= 1.5:  # 比例写法 0.3/0.3/0.2/0.2
        return {key: round(value / total * 100, 1) for key, value in weights.items()}
    return {key: round(value, 1) for key, value in weights.items()}


def _bar(score: float, weight: float | None = None) -> str:
    """黑白进度条：黑色填充 + 灰色底槽。"""
    pct = max(0.0, min(100.0, _num(score)))
    label = f"{pct:.0f}"
    suffix = f'<span class="bar-weight">权重 {weight:.0f}%</span>' if weight is not None else ""
    return (
        '<div class="bar-row">'
        f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
        f'<div class="bar-value">{label}</div>{suffix}'
        "</div>"
    )


def _list_block(title: str, items: list | None, *, numbered: bool = False, empty: str = "暂无") -> str:
    rows = []
    for index, item in enumerate(items or [], start=1):
        if isinstance(item, dict):
            head = _esc(item.get("item") or item.get("title") or "建议项")
            action = _esc(item.get("action") or "")
            extra = f'<div class="item-action">{action}</div>' if action else ""
            rows.append(f"<li><div class=\"item-head\">{head}</div>{extra}</li>")
        else:
            marker = f"{index}. " if numbered else ""
            rows.append(f"<li><div class=\"item-head\">{marker}{_esc(item)}</div></li>")
    body = f'<ul class="item-list">{"".join(rows)}</ul>' if rows else f'<p class="muted">{_esc(empty)}</p>'
    return f'<div class="block"><h3>{_esc(title)}</h3>{body}</div>'


def _kv_table(pairs: list[tuple[str, Any]]) -> str:
    rows = "".join(
        f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>" for key, value in pairs
    )
    return f'<table class="kv">{rows}</table>'


def _metric_table(rows: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f"<tr><td>{_esc(name)}</td><td class=\"num\">{_esc(actual)}</td><td class=\"hint\">{_esc(hint)}</td></tr>"
        for name, actual, hint in rows
    )
    return (
        '<table class="metrics"><thead><tr><th>检查项</th><th class="num">实测</th><th>口径</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _bool_text(value: Any) -> str:
    return "已具备" if value else "缺失"


def _build_cover(detail: dict, generated_at: str, weights: dict[str, float]) -> str:
    scores = {
        "schema": _num((detail.get("schema_analysis") or {}).get("score")),
        "content": _num((detail.get("content_analysis") or {}).get("score")),
        "meta": _num((detail.get("meta_analysis") or {}).get("score")),
        "citation": _num((detail.get("citation_analysis") or {}).get("score")),
    }
    score_cells = "".join(
        '<div class="cover-score-cell">'
        f'<div class="cover-score-label">{_esc(DIMENSION_META[key][0])}</div>'
        f'<div class="cover-score-value">{scores[key]:.0f}</div>'
        f'<div class="cover-score-weight">权重 {weights.get(key, 0):.0f}%</div>'
        "</div>"
        for key in DIMENSION_META
    )
    subject = detail.get("company_name") or detail.get("url") or "未命名对象"
    return f"""
<section class="cover">
  <div class="cover-brand">{_esc(BRAND_TEXT)}</div>
  <div class="cover-main">
    <div class="cover-eyebrow">{_esc(REPORT_TITLE)}</div>
    <h1 class="cover-title">{_esc(subject)}</h1>
    <div class="cover-url">{_esc(detail.get("url") or "")}</div>
    <div class="cover-overall">
      <span class="cover-overall-value">{_fmt_score(detail.get("overall_score"))}</span>
      <span class="cover-overall-unit">/ 100</span>
      <span class="cover-overall-label">综合 GEO 评分</span>
    </div>
    <div class="cover-scores">{score_cells}</div>
  </div>
  <div class="cover-foot">
    <div>报告编号：{_esc(detail.get("id") or "")}</div>
    <div>生成时间：{_esc(generated_at)}</div>
  </div>
</section>
"""


def _build_dimension_sections(detail: dict) -> str:
    schema = detail.get("schema_analysis") or {}
    content = detail.get("content_analysis") or {}
    meta = detail.get("meta_analysis") or {}
    citation = detail.get("citation_analysis") or {}

    schema_found = "、".join(schema.get("found_types") or []) or "无"
    schema_missing = "、".join(schema.get("missing_recommended") or []) or "无"
    schema_block = f"""
<div class="block">
  <h3>结构化数据 · Schema <span class="score-tag">{_fmt_score(schema.get("score"))} / 100</span></h3>
  {_kv_table([
      ("已检测到的类型", schema_found),
      ("建议补充的类型", schema_missing),
      ("JSON-LD 脚本数", schema.get("schema_count", 0)),
      ("推荐类型覆盖率", f"{_num(schema.get('coverage_ratio')):.0f}%"),
  ])}
  {_metric_table([
      ("Organization / WebSite", _bool_text(schema.get("has_org")), "页面主体实体声明"),
      ("FAQPage", _bool_text(schema.get("has_faq")), "问答结构化，利于答案抽取"),
      ("Article", _bool_text(schema.get("has_article")), "内容型页面归属"),
      ("BreadcrumbList", _bool_text(schema.get("has_breadcrumb")), "层级路径声明"),
  ])}
</div>
"""

    meta_missing = "、".join(meta.get("missing") or []) or "无"
    meta_block = f"""
<div class="block">
  <h3>元信息 · Meta <span class="score-tag">{_fmt_score(meta.get("score"))} / 100</span></h3>
  {_kv_table([
      ("缺失项", meta_missing),
      ("预览分（社交/摘要）", _fmt_score(meta.get("preview_score"))),
      ("Meta Description 长度", (meta.get("checks") or {}).get("meta_description_length", 0)),
      ("Title 长度", (meta.get("checks") or {}).get("title_length", 0)),
  ])}
</div>
"""

    content_block = f"""
<div class="block">
  <h3>内容结构 · Content <span class="score-tag">{_fmt_score(content.get("score"))} / 100</span></h3>
  {_metric_table([
      ("H1 数量（应为 1）", str(content.get("h1_count", 0)), "唯一 H1 表达页面主题"),
      ("H2 数量（应 ≥ 2）", str(content.get("h2_count", 0)), "形成清晰层级"),
      ("正文字符数（应 > 800）", str(content.get("character_count", 0)), "去空白字符"),
      ("首段是否直达答案", _bool_text(content.get("first_para_quality")), "首段 > 80 字符且直接回答"),
      ("图片 alt 覆盖率", f"{_num(content.get('image_alt_ratio')):.0f}%", "建议 ≥ 60%"),
      ("问答区块数", str(content.get("faq_like_sections", 0)), "FAQ / 常见问题"),
      ("列表 / 表格数", f"{content.get('list_count', 0)} / {content.get('table_count', 0)}", "结构化可读内容"),
      ("CTA 按钮数", str(content.get("cta_count", 0)), "联系 / 试用 / 定价等"),
  ])}
</div>
"""

    citation_block = f"""
<div class="block">
  <h3>引用与信任 · Citation <span class="score-tag">{_fmt_score(citation.get("score"))} / 100</span></h3>
  {_metric_table([
      ("外部链接数（应 ≥ 3）", str(citation.get("external_link_count", 0)), "外部参考来源"),
      ("权威引用数（应 ≥ 2）", str(citation.get("authority_link_count", 0)), "论文 / 标准 / 官方文档"),
      ("站内链接数", str(citation.get("internal_link_count", 0)), "建议 ≥ 12 形成信息架构"),
      ("社交主页链接", str(citation.get("social_link_count", 0)), "官方主页背书"),
  ])}
</div>
"""

    return schema_block + meta_block + content_block + citation_block


def _build_phase_table(phase_plan: list | None) -> str:
    rows = []
    for item in phase_plan or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f'<td class="phase">{_esc(item.get("phase") or "")}</td>'
            f'<td>{_esc(item.get("title") or "")}</td>'
            f'<td>{_esc(item.get("goal") or "")}</td>'
            f'<td>{_esc(item.get("success_metric") or "")}</td>'
            "</tr>"
        )
    if not rows:
        return '<p class="muted">暂无执行路线。</p>'
    return (
        '<table class="plan"><thead><tr><th>阶段</th><th>事项</th><th>目标</th><th>衡量指标</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def build_report_html(detail: dict, *, generated_at: str | None = None) -> str:
    """把后台报告 payload 渲染成一份黑白印刷风格的 HTML。"""
    if not generated_at:
        from zoneinfo import ZoneInfo

        generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    weights = _normalize_weights(detail.get("rule_config"))
    recommendations = detail.get("recommendations") or {}
    summary = recommendations.get("summary") or {}

    overall = _num(detail.get("overall_score"))
    dims = {
        "schema": _num((detail.get("schema_analysis") or {}).get("score")),
        "content": _num((detail.get("content_analysis") or {}).get("score")),
        "meta": _num((detail.get("meta_analysis") or {}).get("score")),
        "citation": _num((detail.get("citation_analysis") or {}).get("score")),
    }
    overall_rows = "".join(
        '<div class="dim-row">'
        f'<div class="dim-name">{_esc(DIMENSION_META[key][0])}<span class="dim-en">{_esc(DIMENSION_META[key][1])}</span></div>'
        f'{_bar(dims[key], weights.get(key, 0))}'
        "</div>"
        for key in DIMENSION_META
    )

    error_block = ""
    if detail.get("error_message"):
        error_block = f'<div class="block"><h3>错误信息</h3><p class="muted">{_esc(detail["error_message"])}</p></div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{_esc(REPORT_TITLE)} - {_esc(detail.get('url') or '')}</title>
<style>
  @page {{ size: A4 landscape; margin: 14mm 12mm; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "PingFang SC", sans-serif;
    color: #111; background: #fff; font-size: 11pt; line-height: 1.55;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }}
  h1, h2, h3, h4 {{ margin: 0 0 6px; font-weight: 700; color: #000; }}
  h3 {{ font-size: 13pt; border-left: 4px solid #000; padding-left: 8px; margin-bottom: 10px; }}
  p {{ margin: 0 0 8px; }}
  .muted {{ color: #555; }}

  /* ===== 封面 ===== */
  .cover {{ height: 178mm; display: flex; flex-direction: column; justify-content: space-between; page-break-after: always; }}
  .cover-brand {{ font-size: 12pt; letter-spacing: .06em; font-weight: 700; border-bottom: 2px solid #000; padding-bottom: 8px; }}
  .cover-main {{ padding-top: 18mm; }}
  .cover-eyebrow {{ font-size: 14pt; letter-spacing: .3em; color: #333; margin-bottom: 10px; }}
  .cover-title {{ font-size: 30pt; line-height: 1.2; margin: 0 0 4px; }}
  .cover-url {{ font-size: 12pt; color: #444; margin-bottom: 16mm; word-break: break-all; }}
  .cover-overall {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 10mm; }}
  .cover-overall-value {{ font-size: 52pt; font-weight: 800; line-height: 1; }}
  .cover-overall-unit {{ font-size: 16pt; color: #333; }}
  .cover-overall-label {{ font-size: 11pt; color: #444; margin-left: 12px; }}
  .cover-scores {{ display: flex; gap: 10px; }}
  .cover-score-cell {{ flex: 1; border: 1px solid #000; padding: 10px 12px; }}
  .cover-score-label {{ font-size: 10pt; color: #333; }}
  .cover-score-value {{ font-size: 22pt; font-weight: 700; }}
  .cover-score-weight {{ font-size: 9pt; color: #555; }}
  .cover-foot {{ display: flex; justify-content: space-between; font-size: 9.5pt; color: #444; border-top: 1px solid #000; padding-top: 6px; }}

  /* ===== 正文 ===== */
  .section {{ break-inside: avoid; page-break-inside: avoid; margin-bottom: 14px; }}
  .section-title {{ font-size: 15pt; border-bottom: 2px solid #000; padding-bottom: 6px; margin-bottom: 12px; }}
  .block {{ break-inside: avoid; page-break-inside: avoid; border: 1px solid #bbb; padding: 12px 14px; margin-bottom: 12px; }}
  .score-tag {{ font-size: 10pt; font-weight: 500; color: #333; margin-left: 6px; }}
  .summary-grid {{ display: flex; gap: 12px; margin-bottom: 12px; }}
  .summary-card {{ flex: 1; border: 1px solid #000; padding: 10px 12px; }}
  .summary-card .k {{ font-size: 9pt; color: #555; margin-bottom: 4px; }}
  .summary-card .v {{ font-size: 11pt; font-weight: 600; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 10pt; }}
  th, td {{ border: 1px solid #bbb; padding: 5px 8px; text-align: left; vertical-align: top; }}
  thead th {{ background: #f0f0f0; font-weight: 700; }}
  table.kv th {{ width: 32%; background: #f7f7f7; font-weight: 600; }}
  td.num, th.num {{ text-align: right; white-space: nowrap; }}
  td.hint {{ color: #555; }}
  td.phase {{ font-weight: 700; white-space: nowrap; }}

  .dim-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .dim-name {{ width: 130px; font-weight: 600; }}
  .dim-en {{ font-size: 9pt; color: #555; margin-left: 6px; font-weight: 400; }}
  .bar-row {{ flex: 1; display: flex; align-items: center; gap: 8px; }}
  .bar-track {{ flex: 1; height: 12px; background: #e6e6e6; border: 1px solid #999; }}
  .bar-fill {{ height: 100%; background: #111; }}
  .bar-value {{ width: 34px; text-align: right; font-weight: 700; }}
  .bar-weight {{ font-size: 9pt; color: #555; width: 74px; text-align: right; }}

  .item-list {{ margin: 0; padding-left: 18px; }}
  .item-list li {{ margin-bottom: 6px; break-inside: avoid; }}
  .item-head {{ font-weight: 600; }}
  .item-action {{ color: #333; }}

  .two-col {{ display: flex; gap: 12px; }}
  .two-col > * {{ flex: 1; }}
</style>
</head>
<body>
{_build_cover(detail, generated_at, weights)}

<section class="section">
  <h2 class="section-title">一、报告摘要</h2>
  <div class="summary-grid">
    <div class="summary-card"><div class="k">结论</div><div class="v">{_esc(summary.get("headline") or "—")}</div></div>
    <div class="summary-card"><div class="k">首要动作</div><div class="v">{_esc(summary.get("priority_action") or "—")}</div></div>
  </div>
  <div class="block"><p>{_esc(summary.get("overview") or "—")}</p></div>
</section>

<section class="section">
  <h2 class="section-title">二、综合评分</h2>
  <div class="block">{overall_rows}</div>
</section>

<section class="section">
  <h2 class="section-title">三、维度明细</h2>
  {_build_dimension_sections(detail)}
</section>

<section class="section">
  <h2 class="section-title">四、优势与短板</h2>
  <div class="two-col">
    {_list_block("已具备优势", recommendations.get("strengths"))}
    {_list_block("主要短板", recommendations.get("gaps"))}
  </div>
</section>

<section class="section">
  <h2 class="section-title">五、优化建议</h2>
  {_list_block("P0 紧急（先做）", recommendations.get("urgent"), numbered=True)}
  {_list_block("P1 建议", recommendations.get("recommended"), numbered=True)}
  {_list_block("P2 可选", recommendations.get("optional"), numbered=True)}
</section>

<section class="section">
  <h2 class="section-title">六、执行路线</h2>
  <div class="block">{_build_phase_table(recommendations.get("phase_plan"))}</div>
</section>

{error_block}
</body>
</html>
"""


def render_pdf(html: str, *, landscape: bool = True, timeout_ms: int = 60000) -> bytes:
    """用 Chromium 把 HTML 渲染成 PDF。必须在装有 Chromium 的容器里调用（crawler）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load", timeout=timeout_ms)
            page.emulate_media(media="print")
            pdf_bytes = page.pdf(
                format="A4",
                landscape=landscape,
                print_background=True,
                margin={"top": "14mm", "right": "12mm", "bottom": "16mm", "left": "12mm"},
                display_header_footer=True,
                header_template=(
                    '<div style="width:100%;font-size:8px;color:#444;'
                    'padding:0 12mm;display:flex;justify-content:space-between;'
                    'font-family:sans-serif;">'
                    f'<span>{BRAND_TEXT}</span><span>{REPORT_TITLE}</span>'
                    "</div>"
                ),
                footer_template=(
                    '<div style="width:100%;font-size:8px;color:#444;'
                    'padding:0 12mm;display:flex;justify-content:space-between;'
                    'font-family:sans-serif;">'
                    f'<span>{BRAND_TEXT}</span>'
                    '<span>第 <span class="pageNumber"></span> / <span class="totalPages"></span> 页</span>'
                    "</div>"
                ),
            )
        finally:
            browser.close()
    return pdf_bytes
