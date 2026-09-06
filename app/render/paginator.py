"""
Text paginator — split analysis text into pages respecting brand spec.
Slice 4 · docs/ARCHITECTURE/09_templates.md §3

Profile-aware limits:
  a4             : 1200–1600 chars / page (V9.2 §3.3)
  messenger_card : pixel pack into safe area (+ char ceiling fallback)

Rules:
- Never break in the middle of a section heading
- Never break mid-sentence (break after '.', '!', '?', '\\n\\n')
- Never orphan numbered list markers (e.g. "6." alone on a page)
- Merge underfilled messenger pages when combined content still fits
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.render.branding import BrandSpec, DEFAULT_BRAND

_ORPHAN_NUMBERED_CHUNK = re.compile(r"^\d+\.?\s*$")


@dataclass
class PageContent:
    page_num: int          # 1-indexed
    total_pages: int
    sections: list[dict]   # [{"heading": str | None, "body": str}]
    char_count: int
    topic: str | None = None          # derived card topic (first heading or None)
    section_title: str | None = None  # explicit section title override
    is_continuation: bool = False     # True when this page continues a prior split section


def _estimate_chars(brand: BrandSpec) -> tuple[int, int]:
    """Return (min_chars, max_chars) per page from brand spec."""
    return brand.chars_per_page_min, brand.chars_per_page_max


def _split_into_paragraphs(text: str) -> list[str]:
    """Split text by double newline, preserving single-line headings."""
    paras = re.split(r"\n{2,}", text.strip())
    return [p.strip() for p in paras if p.strip()]


def _is_heading(para: str) -> bool:
    return para.startswith("#") or (len(para) < 80 and not para.endswith("."))


def paginate_analysis(
    sections: dict[str, str],
    brand: BrandSpec = DEFAULT_BRAND,
) -> list[PageContent]:
    """
    Split analysis sections dict into pages respecting brand limits.

    Args:
        sections: {heading_slug: body_text} from analyzer._parse_sections()
        brand: BrandSpec for per-page limits

    Returns:
        list of PageContent, one per rendered page.
    """
    if brand.render_profile == "messenger_card":
        pages = _paginate_messenger_by_pixels(sections, brand)
    else:
        pages = _paginate_by_chars(sections, brand)
    return _finalize_pages(pages)


def _finalize_pages(pages: list[PageContent]) -> list[PageContent]:
    total = len(pages) or 1
    for p in pages:
        p.total_pages = total
        if p.topic is None:
            for sec in p.sections:
                h = sec.get("heading")
                if h:
                    p.topic = h
                    break

    if not pages:
        pages = [PageContent(
            page_num=1, total_pages=1,
            sections=[{"heading": None, "body": "(nội dung đang được chuẩn bị)"}],
            char_count=30,
        )]
    return pages


def _section_char_count(sections: list[dict]) -> int:
    return sum(
        len(s.get("body", "")) + len(s.get("heading") or "")
        for s in sections
    )


def _paginate_by_chars(
    sections: dict[str, str],
    brand: BrandSpec,
) -> list[PageContent]:
    """Char-budget pagination (A4 / archive profile)."""
    min_chars, max_chars = _estimate_chars(brand)

    # Build ordered list of (heading, body) pairs
    items: list[tuple[str | None, str]] = []
    for key, body in sections.items():
        if key.startswith("_"):
            items.append((None, body))
        else:
            # Convert slug back to display heading
            heading = key.replace("_", " ").title()
            items.append((heading, body))

    pages: list[PageContent] = []
    current_sections: list[dict] = []
    current_chars = 0

    for heading, body in items:
        item_chars = len(body) + (len(heading) if heading else 0)
        heading_alone_too_big = item_chars > max_chars

        if current_chars > 0 and current_chars + item_chars > max_chars:
            # Flush current page
            pages.append(PageContent(
                page_num=len(pages) + 1,
                total_pages=0,  # filled below
                sections=current_sections,
                char_count=current_chars,
            ))
            current_sections = []
            current_chars = 0

        # If single item is very long, split its body
        if item_chars > max_chars:
            chunks = _split_long_body(body, max_chars - (len(heading) if heading else 0))
            for i, chunk in enumerate(chunks):
                h = heading if i == 0 else None
                pages.append(PageContent(
                    page_num=len(pages) + 1,
                    total_pages=0,
                    sections=[{"heading": h, "body": chunk}],
                    char_count=len(chunk) + (len(heading) if h else 0),
                    # Continuation pages inherit topic but are marked as such
                    topic=heading,
                    is_continuation=(i > 0),
                ))
            continue

        current_sections.append({"heading": heading, "body": body})
        current_chars += item_chars

    # Flush last page
    if current_sections:
        pages.append(PageContent(
            page_num=len(pages) + 1,
            total_pages=0,
            sections=current_sections,
            char_count=current_chars,
        ))

    return pages


def _paginate_messenger_by_pixels(
    sections: dict[str, str],
    brand: BrandSpec,
) -> list[PageContent]:
    """Pack messenger cards by measured pixel height."""
    from app.render.layout import (
        PageLayoutPlan,
        content_pixel_budget,
        measure_page_content_height,
        measure_section_block_height,
        split_body_by_pixel_budget,
    )
    from app.render.renderer import _clean_markdown_for_render

    plan = PageLayoutPlan(
        body_font_size=brand.font_size_body,
        heading_font_size=brand.font_size_heading,
        line_height_ratio=brand.line_height_ratio,
        section_gap=32,
    )
    budget = content_pixel_budget(brand)
    _, max_chars = _estimate_chars(brand)

    items: list[tuple[str | None, str]] = []
    for key, body in sections.items():
        if key.startswith("_"):
            items.append((None, body))
        else:
            heading = key.replace("_", " ").title()
            items.append((heading, body))

    pages: list[PageContent] = []
    current_sections: list[dict] = []
    current_chars = 0

    def _flush() -> None:
        nonlocal current_sections, current_chars
        if not current_sections:
            return
        pages.append(PageContent(
            page_num=len(pages) + 1,
            total_pages=0,
            sections=current_sections,
            char_count=current_chars,
        ))
        current_sections = []
        current_chars = 0

    def _current_height(extra_heading: str | None = None, extra_body: str = "") -> int:
        cleaned = [
            {
                "heading": _clean_markdown_for_render(s["heading"]) if s.get("heading") else None,
                "body": _clean_markdown_for_render(s.get("body", "")),
            }
            for s in current_sections
        ]
        if extra_heading or extra_body:
            cleaned = cleaned + [{
                "heading": _clean_markdown_for_render(extra_heading) if extra_heading else None,
                "body": _clean_markdown_for_render(extra_body),
            }]
        probe = PageContent(
            page_num=1, total_pages=1, sections=cleaned, char_count=0,
        )
        return measure_page_content_height(probe, brand, plan)

    for heading, body in items:
        clean_body = _clean_markdown_for_render(body)
        clean_heading = _clean_markdown_for_render(heading) if heading else None
        item_chars = len(body) + (len(heading) if heading else 0)
        block_h = measure_section_block_height(clean_heading, clean_body, brand, plan)

        if block_h > budget:
            _flush()
            chunks = split_body_by_pixel_budget(
                clean_body, budget, brand, heading=clean_heading, plan=plan,
            )
            if not chunks:
                chunks = _split_long_body(body, max_chars - (len(heading) if heading else 0))
            for i, chunk in enumerate(chunks):
                h = clean_heading if i == 0 else None
                pages.append(PageContent(
                    page_num=len(pages) + 1,
                    total_pages=0,
                    sections=[{"heading": h, "body": chunk}],
                    char_count=len(chunk) + (len(clean_heading) if h else 0),
                    topic=heading,
                    is_continuation=(i > 0),
                ))
            continue

        if current_sections and _current_height(clean_heading, clean_body) > budget:
            _flush()

        current_sections.append({"heading": heading, "body": body})
        current_chars += item_chars

    _flush()
    pages = _merge_underfilled_messenger_pages(pages, brand)
    return _merge_trailing_short_page(pages, brand)


def _merge_underfilled_messenger_pages(
    pages: list[PageContent],
    brand: BrandSpec,
) -> list[PageContent]:
    """Merge consecutive short pages when combined content still fits one card."""
    from app.render.layout import (
        PageLayoutPlan,
        content_pixel_budget,
        measure_page_content_height,
    )
    from app.render.renderer import _clean_markdown_for_render

    if len(pages) < 2:
        return pages

    plan = PageLayoutPlan(
        body_font_size=brand.font_size_body,
        heading_font_size=brand.font_size_heading,
        line_height_ratio=brand.line_height_ratio,
        section_gap=32,
    )
    budget = content_pixel_budget(brand)
    merged: list[PageContent] = []
    i = 0
    while i < len(pages):
        page = pages[i]
        cleaned = [
            {
                "heading": _clean_markdown_for_render(s["heading"]) if s.get("heading") else None,
                "body": _clean_markdown_for_render(s.get("body", "")),
            }
            for s in page.sections
        ]
        used = measure_page_content_height(
            PageContent(1, 1, cleaned, 0), brand, plan,
        )
        fill = used / max(budget, 1)

        if fill < 0.65 and i + 1 < len(pages):
            nxt = pages[i + 1]
            combined_sections = page.sections + nxt.sections
            combined_clean = [
                {
                    "heading": _clean_markdown_for_render(s["heading"]) if s.get("heading") else None,
                    "body": _clean_markdown_for_render(s.get("body", "")),
                }
                for s in combined_sections
            ]
            combined_h = measure_page_content_height(
                PageContent(1, 1, combined_clean, 0), brand, plan,
            )
            if combined_h <= budget:
                merged.append(PageContent(
                    page_num=len(merged) + 1,
                    total_pages=0,
                    sections=combined_sections,
                    char_count=_section_char_count(combined_sections),
                    topic=page.topic or nxt.topic,
                    is_continuation=page.is_continuation,
                ))
                i += 2
                continue

        merged.append(PageContent(
            page_num=len(merged) + 1,
            total_pages=0,
            sections=page.sections,
            char_count=page.char_count,
            topic=page.topic,
            section_title=page.section_title,
            is_continuation=page.is_continuation,
        ))
        i += 1

    return merged


def _merge_trailing_short_page(
    pages: list[PageContent],
    brand: BrandSpec,
) -> list[PageContent]:
    """Merge a tiny last page (e.g. GPT upsell tail) into the previous card."""
    from app.render.layout import (
        PageLayoutPlan,
        content_pixel_budget,
        measure_page_content_height,
    )
    from app.render.renderer import _clean_markdown_for_render

    if len(pages) < 2:
        return pages

    last = pages[-1]
    prev = pages[-2]
    last_chars = _section_char_count(last.sections)
    if last_chars > 280:
        return pages

    combined_sections = prev.sections + last.sections
    plan = PageLayoutPlan(
        body_font_size=brand.font_size_body,
        heading_font_size=brand.font_size_heading,
        line_height_ratio=brand.line_height_ratio,
        section_gap=32,
    )
    budget = content_pixel_budget(brand)
    combined_clean = [
        {
            "heading": _clean_markdown_for_render(s["heading"]) if s.get("heading") else None,
            "body": _clean_markdown_for_render(s.get("body", "")),
        }
        for s in combined_sections
    ]
    combined_h = measure_page_content_height(
        PageContent(1, 1, combined_clean, 0), brand, plan,
    )
    if combined_h > budget:
        return pages

    merged = pages[:-2] + [
        PageContent(
            page_num=0,
            total_pages=0,
            sections=combined_sections,
            char_count=_section_char_count(combined_sections),
            topic=prev.topic or last.topic,
            section_title=prev.section_title or last.section_title,
            is_continuation=prev.is_continuation,
        )
    ]
    for idx, p in enumerate(merged, 1):
        p.page_num = idx
    return merged


def _is_numbered_list_dot(text: str, dot_pos: int) -> bool:
    """True when dot_pos is the '.' in a line-leading 'N. ' list marker."""
    if dot_pos <= 0 or dot_pos >= len(text) or text[dot_pos] != ".":
        return False
    j = dot_pos - 1
    while j >= 0 and text[j].isdigit():
        j -= 1
    if j == dot_pos - 1:
        return False
    line_start = text.rfind("\n", 0, dot_pos) + 1
    prefix = text[line_start:dot_pos].strip()
    return bool(re.fullmatch(r"\d+", prefix))


def _find_sentence_cut(window: str) -> int:
    """Return exclusive end index for the first part; 0 means hard-cut fallback."""
    for sep in (".\n", "!\n", "?\n", ". ", "! ", "? "):
        idx = len(window)
        while idx > 0:
            pos = window.rfind(sep, 0, idx)
            if pos < 0:
                break
            if sep.startswith(".") and _is_numbered_list_dot(window, pos):
                idx = pos
                continue
            return pos + len(sep)
    return 0


def _find_word_boundary_cut(window: str) -> int:
    """Prefer breaking at whitespace so words are not split across pages."""
    if not window:
        return 0
    pos = window.rfind(" ", 0, len(window))
    if pos > 0:
        return pos + 1
    pos = window.rfind("\n", 0, len(window))
    if pos > 0:
        return pos + 1
    return 0


def _merge_orphan_chunks(chunks: list[str]) -> list[str]:
    """Merge numbered markers (e.g. '6.') forward into the next chunk."""
    if not chunks:
        return chunks
    merged: list[str] = []
    pending = ""
    for chunk in chunks:
        piece = chunk.strip()
        if not piece:
            continue
        if _ORPHAN_NUMBERED_CHUNK.match(piece):
            pending = (pending + " " + piece).strip() if pending else piece
            continue
        if pending:
            piece = f"{pending} {piece}".strip()
            pending = ""
        merged.append(piece)
    if pending:
        if merged:
            merged[-1] = f"{merged[-1]} {pending}".strip()
        else:
            merged.append(pending)
    return merged


def _split_long_body(text: str, max_chars: int) -> list[str]:
    """
    Split a long body text into chunks ≤ max_chars.
    Prefer sentence boundaries (., !, ?, \\n) but never after numbered list markers.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()

    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = _find_sentence_cut(window)
        if cut <= 0:
            cut = _find_word_boundary_cut(window)
        if cut <= 0:
            cut = max_chars

        head = remaining[:cut].strip()
        if head:
            chunks.append(head)
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return _merge_orphan_chunks(chunks)
