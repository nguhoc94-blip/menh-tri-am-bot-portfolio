"""
Pillow-based renderer for Demo Bot V9.

Supports two render profiles:
  - "a4"            : 2480×3508 px (archive/PDF)
  - "messenger_card": 1080×1920 px (9:16 mobile-first Messenger cards)

Each profile uses the same rendering logic but a different BrandSpec.
Default for Messenger sends is MESSENGER_CARD_BRAND.

docs/ARCHITECTURE/09_templates.md
"""
from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.render.branding import BrandSpec, DEFAULT_BRAND
from app.render.paginator import PageContent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_ACCENT_LINE_HEIGHT_PX = 6

# V9 §8.3 — mode label drawn on each page
_MODE_LABELS: dict[str, str] = {
    "palm": "Bản Luận Giải · Chỉ Tay",
    "face": "Bản Luận Giải · Tướng Mặt",
    "DEMO": "Bản Luận Giải · Tử Vi",
    "combined": "Bản Luận Giải · Tổng Hợp",
}


# ── Typography cleanup ────────────────────────────────────────────────────────

def _clean_markdown_for_render(text: str) -> str:
    """
    Strip markdown syntax so rendered images contain clean Vietnamese text.

    Rules:
      **bold**  → bold (text only)
      *italic*  → text only
      # Heading → Heading
      ---       → removed (replaced by nothing; section dividers handled visually)
      > quote   → text only (no '>' prefix)
      - bullet  → • bullet
    """
    if not text:
        return text

    # Remove heading markers: ##, ###, etc.
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Bold+italic combined: ***text*** or ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text, flags=re.DOTALL)

    # Bold: **text** or __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text, flags=re.DOTALL)

    # Italic: *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_(.+?)_", r"\1", text, flags=re.DOTALL)

    # Horizontal rules: --- or *** or ___  (line must be only those chars)
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # Block-quotes: leading >
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # Bullet points: "- " or "* " at line start → "• "
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Collapse multiple blank lines to one
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _extract_intro_quote(page: "PageContent", max_len: int = 120) -> str:  # type: ignore[name-defined]
    """Extract a short intro quote (≤ max_len chars) from the first section body.

    Used to generate a teaser or subtitle from a rendered card's content.
    Returns an empty string if no suitable text is found.
    """
    for section in (page.sections or []):
        body = section.get("body", "").strip() if isinstance(section, dict) else ""
        if not body:
            continue
        body = _clean_markdown_for_render(body)
        # Split into sentences on Vietnamese/common sentence endings
        sentences = re.split(r"[.!?。]\s+", body)
        for sentence in sentences:
            candidate = sentence.strip()
            if len(candidate) >= 10:
                if len(candidate) <= max_len:
                    return candidate
                return candidate[:max_len - 1].rsplit(" ", 1)[0] + "…"
    return ""


# ── Pixel-accurate text wrapping ──────────────────────────────────────────────

def _wrap_text_by_pixels(draw, text: str, font, max_width: int) -> list[str]:
    """
    Wrap *text* to fit within *max_width* pixels using real font metrics.

    Strategy:
      - Split on existing newlines to preserve paragraph breaks.
      - Within each paragraph, accumulate words until the line would overflow.
      - Bullet lines (starting with '•') are indented on continuation.
      - A single word wider than max_width is never split — it renders as-is
        (avoids crashes on very-long URLs or unbreakable strings).

    Returns a list of display lines (empty string = blank line between paras).
    """
    if not text:
        return []

    lines: list[str] = []

    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            lines.append("")
            continue

        is_bullet = para.startswith("•")
        indent = "  " if is_bullet else ""   # 2-space continuation indent

        words = para.split()
        current = ""

        for word in words:
            candidate = (current + " " + word).strip() if current else word
            try:
                bbox = draw.textbbox((0, 0), candidate, font=font)
                w = bbox[2] - bbox[0]
            except Exception:
                # Fallback: estimate via font.size
                size = getattr(font, "size", 16)
                w = len(candidate) * size // 2

            if w <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = indent + word  # start new line with indent

        if current:
            lines.append(current)

    return lines


# ── Canvas construction ───────────────────────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _load_font(path: str, size: int):
    """Load TTF font, return ImageFont. Falls back to default if path missing."""
    from PIL import ImageFont
    p = Path(path)
    if p.exists():
        try:
            return ImageFont.truetype(str(p), size)
        except Exception as exc:
            logger.warning("font_load_failed path=%s err=%s", path, exc)
    return ImageFont.load_default()


def _resolve_template_path(brand: BrandSpec, variant: str) -> str:
    """Pick template PNG by variant; falls back to default if alt missing."""
    if variant == "alt" and Path(brand.template_alt).exists():
        return brand.template_alt
    return brand.template_default


def _make_canvas(brand: BrandSpec, *, template_variant: str = "default"):
    """
    Create background canvas — template PNG resized/center-cropped to canvas
    dimensions, or solid colour fallback.

    For messenger_card: if template is taller than wide (portrait) we center-
    crop; if wider (landscape) we scale+crop from the top.
    """
    from PIL import Image
    template_path = Path(_resolve_template_path(brand, template_variant))
    if template_path.exists():
        try:
            bg = Image.open(template_path).convert("RGB")
            tw, th = bg.size
            cw, ch = brand.canvas_width, brand.canvas_height

            if bg.size != (cw, ch):
                # Scale so the template fills the canvas, then center-crop
                scale = max(cw / tw, ch / th)
                new_w = int(tw * scale)
                new_h = int(th * scale)
                bg = bg.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - cw) // 2
                top = (new_h - ch) // 2
                bg = bg.crop((left, top, left + cw, top + ch))
            return bg
        except Exception as exc:
            logger.warning("template_load_failed path=%s err=%s", template_path, exc)

    bg_rgb = _hex_to_rgb(brand.color_background_fallback)
    return Image.new("RGB", (brand.canvas_width, brand.canvas_height), bg_rgb)


# ── Overlay: semi-transparent text card (messenger_card profile only) ─────────

def _draw_text_card_overlay(
    canvas,
    brand: BrandSpec,
    *,
    card_bottom_y: int | None = None,
) -> None:
    """
    Draw a rounded-rectangle semi-transparent overlay card behind the text area.
    Only applied when render_profile == "messenger_card".
    """
    try:
        from PIL import Image, ImageDraw

        card_x = brand.safe_area_x - 20
        card_y = brand.safe_area_y_start - 16
        card_w = brand.canvas_width - 2 * card_x
        bottom = card_bottom_y if card_bottom_y is not None else brand.safe_area_y_end
        card_h = bottom - card_y + 16
        radius = 32

        # Build card on RGBA layer then composite
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        card_fill = (252, 246, 240, 218)  # warm ivory, ~85% opacity
        odraw.rounded_rectangle(
            [card_x, card_y, card_x + card_w, card_y + card_h],
            radius=radius,
            fill=card_fill,
        )
        # Very subtle warm border
        border_color = (231, 212, 189, 160)
        odraw.rounded_rectangle(
            [card_x, card_y, card_x + card_w, card_y + card_h],
            radius=radius,
            outline=border_color,
            width=2,
        )
        canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"))
    except Exception as exc:
        logger.warning("text_card_overlay_failed err=%s", exc)


# ── Logo ──────────────────────────────────────────────────────────────────────

def _draw_logo(canvas, brand: BrandSpec) -> None:
    """Paste logo — top-right for A4, top-left for messenger_card. Never raises."""
    try:
        from PIL import Image
        logo_path = Path(brand.logo_path)
        if not logo_path.exists():
            return
        logo = Image.open(logo_path).convert("RGBA")
        size = brand.logo_size or (240, 240)
        logo = logo.resize(size, Image.LANCZOS)

        if brand.render_profile == "messenger_card":
            margin = max(20, brand.safe_area_x // 3)
            pos = (margin, margin)
        else:
            margin = max(60, brand.safe_area_x // 4)
            pos = (brand.canvas_width - brand.safe_area_x - size[0], margin)

        canvas.paste(logo, pos, logo)
    except Exception as exc:
        logger.warning("logo_draw_failed err=%s", exc)


# ── Card topic / heading helpers ─────────────────────────────────────────────

def _clean_heading_text(text: str) -> str:
    """
    Strip all markdown syntax from a heading string and return clean display text.

    Rules:
    - Remove #, ##, ###... prefixes
    - Remove **bold**, *italic*, __underline__ markers
    - Remove --- horizontal rules
    - Remove > blockquote markers
    - Strip GPT numbered-section prefixes (e.g. "3. " or "3) ") — avoids header "3." after truncation
    - Normalize whitespace (no double spaces)
    - Truncate at 68 chars on a clean word/punctuation boundary — never with "..."
    - Empty/whitespace-only input returns "Bản luận giải"
    """
    if not text or not text.strip():
        return "Bản luận giải"
    t = text.strip()
    # Remove heading markers
    t = re.sub(r"^#{1,6}\s+", "", t)
    # Numbered list / section index from GPT (e.g. "3. Tổng khí chất" → "Tổng khí chất")
    t = re.sub(r"^\d+[\.\)]\s+", "", t)
    # Bold+italic combined
    t = re.sub(r"\*{3}(.+?)\*{3}", r"\1", t)
    t = re.sub(r"_{3}(.+?)_{3}", r"\1", t)
    # Bold
    t = re.sub(r"\*{2}(.+?)\*{2}", r"\1", t)
    t = re.sub(r"_{2}(.+?)_{2}", r"\1", t)
    # Italic
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"_(.+?)_", r"\1", t)
    # Horizontal rules
    t = re.sub(r"^[-*_]{3,}\s*$", "", t, flags=re.MULTILINE)
    # Blockquotes
    t = re.sub(r"^>\s?", "", t, flags=re.MULTILINE)
    # Normalize whitespace
    t = " ".join(t.split())
    # Truncate cleanly at word boundary, not mid-word
    if len(t) > 68:
        cut = t.rfind("·", 0, 68)
        if cut < 20:
            cut = t.rfind(" ", 0, 62)
        # Never truncate to bare "N" or "N." (happens when title is "3. Long title...")
        if cut > 0 and re.fullmatch(r"\d+\.?", t[:cut].strip()):
            next_cut = t.rfind(" ", cut + 1, 68)
            if next_cut > cut:
                cut = next_cut
        if cut > 0:
            t = t[:cut].rstrip()
        else:
            t = t[:65].rstrip()
    return t or "Bản luận giải"


_KEYWORD_TOPIC_MAP: list[tuple[list[str], str]] = [
    (["sự nghiệp", "công việc", "quan lộc", "nghề nghiệp"], "Sự nghiệp & công việc"),
    (["tài chính", "tiền bạc", "tài bạch", "tài sản", "giàu"], "Tài chính & tài sản"),
    (["tình cảm", "phu thê", "hôn nhân", "tình yêu"], "Tình cảm & đồng hành"),
    (["sức khỏe", "tật ách"], "Sức khỏe & cân bằng"),
    (["đại vận", "tiểu vận", "23–32", "33–42", "43–52", "giai đoạn"], "Đại vận & giai đoạn"),
    (["mệnh", "thân cung", "tính cách", "bản mệnh"], "Tổng quan bản mệnh"),
]


def _derive_card_topic(
    page: PageContent,
    *,
    user_focus: str | None = None,
    page_index: int = 0,
) -> str:
    """
    Derive a clean, human-readable topic/title for a Messenger card.

    Priority order:
    1. page.section_title (explicit override)
    2. page.topic (first section heading, set by paginator)
    3. user_focus keyword → canonical topic map
    4. First markdown heading found in body text
    5. Keyword classifier on page body
    6. Fallback: "Bản luận giải"

    Never returns an excerpt of body text.
    Never returns text containing **, ###, ---, or "...".
    """
    # Priority 1: explicit section_title override
    if page.section_title:
        return _clean_heading_text(page.section_title)

    # Priority 2: paginator-derived topic (first section heading)
    if page.topic:
        return _clean_heading_text(page.topic)

    # Priority 3: user_focus → canonical topic
    if user_focus:
        fl = user_focus.lower()
        for keywords, label in _KEYWORD_TOPIC_MAP:
            for kw in keywords:
                if kw in fl:
                    return label

    # Priority 4: first markdown heading found in any section body
    for sec in page.sections:
        body = sec.get("body") or ""
        for line in body.split("\n"):
            ls = line.strip()
            if ls.startswith("#"):
                cleaned = re.sub(r"^#{1,6}\s+", "", ls).strip()
                if cleaned:
                    return _clean_heading_text(cleaned)

    # Priority 5: keyword classifier on all body text
    all_body = " ".join(sec.get("body", "") for sec in page.sections).lower()
    for keywords, label in _KEYWORD_TOPIC_MAP:
        for kw in keywords:
            if kw in all_body:
                return label

    return "Bản luận giải"


# ── Commercial footer ─────────────────────────────────────────────────────────

def _draw_commercial_footer(draw, brand: BrandSpec, top_y: int) -> None:
    """
    Render bank block + affiliate line driven by env vars.
    Skipped for messenger_card (footer space is minimal).
    """
    if brand.render_profile == "messenger_card":
        return  # no commercial block on mobile cards

    bank_block = (os.environ.get("MESSENGER_FOOTER_BANK_BLOCK") or "").strip()
    affiliate_line = (os.environ.get("MESSENGER_FOOTER_AFFILIATE_LINE") or "").strip()
    if not bank_block and not affiliate_line:
        return

    font_size = max(20, brand.font_size_body - 10)
    line_height = int(font_size * 1.25)
    font_footer = _load_font(brand.font_body, font_size)
    color = _hex_to_rgb(brand.color_accent_gold)
    body_color = _hex_to_rgb(brand.color_body)

    max_footer_height = int(brand.canvas_height * 0.12)
    footer_bottom = top_y + max_footer_height
    x = brand.safe_area_x
    y = top_y

    if bank_block:
        for line in bank_block.replace("\\n", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if y + line_height > footer_bottom:
                return
            draw.text((x, y), line, font=font_footer, fill=body_color)
            y += line_height

    if affiliate_line and y + line_height <= footer_bottom:
        y += 6
        draw.text((x, y), affiliate_line, font=font_footer, fill=color)


def _draw_card_referral_line(draw, brand: BrandSpec) -> None:
    """
    Small, unobtrusive watermark near the bottom edge of messenger_card pages
    so a screenshot/share still points a new reader back to the bot.

    Kept deliberately tiny and low-contrast (body color, not gold accent) so
    it does not compete with the reading content. Skipped for the a4 profile
    and can be disabled entirely via MESSENGER_CARD_REFERRAL_ENABLED=false.
    """
    if brand.render_profile != "messenger_card":
        return

    enabled_raw = (os.environ.get("MESSENGER_CARD_REFERRAL_ENABLED") or "true").strip().lower()
    if enabled_raw in ("false", "0", "no", "off"):
        return

    text = (os.environ.get("MESSENGER_CARD_REFERRAL_TEXT") or "").strip()
    if not text:
        text = "Demo Bot · Nhắn tin Messenger để xem lá số của bạn"

    font_size = max(16, brand.font_size_body - 16)
    font = _load_font(brand.font_body, font_size)
    color = _hex_to_rgb(brand.color_body)
    y = brand.canvas_height - int(font_size * 1.8)

    draw.text(
        (brand.canvas_width // 2, y),
        text,
        font=font,
        fill=color,
        anchor="mt",
    )


# ── Wrapped text renderer (pixel-accurate) ───────────────────────────────────

def _draw_wrapped_text(
    *,
    draw,
    text: str,
    x: int,
    y: int,
    font,
    fill: tuple,
    max_width: int,
    line_height: int,
    max_y: int,
    canvas_width: int,
) -> int:
    """
    Draw pixel-accurately word-wrapped text. Returns updated y cursor.
    Stops drawing when y + line_height would exceed max_y.
    """
    lines = _wrap_text_by_pixels(draw, text, font, max_width)

    for line in lines:
        if y + line_height > max_y:
            break
        if line:
            draw.text((x, y), line, font=font, fill=fill)
        y += line_height

    return y


_CONTINUATION_TOPIC_SUFFIX = " · tiếp theo"


def _draw_messenger_topic_title(
    *,
    draw,
    topic: str,
    page: PageContent,
    brand: BrandSpec,
    layout_plan,
    x: int,
    y: int,
    fill: tuple,
    max_width: int,
    max_y: int,
) -> int:
    """Draw card topic; continuation pages use a smaller ' · tiếp theo' suffix."""
    from app.render.layout import topic_continuation_suffix_font_size, topic_title_font_size

    main_size = topic_title_font_size(page, brand, layout_plan)
    font_main = _load_font(brand.font_heading, main_size)
    lh_main = int(main_size * layout_plan.line_height_ratio)

    if page.is_continuation and topic.endswith(_CONTINUATION_TOPIC_SUFFIX):
        base = topic[: -len(_CONTINUATION_TOPIC_SUFFIX)]
        suffix = _CONTINUATION_TOPIC_SUFFIX
        suffix_size = topic_continuation_suffix_font_size(brand)
        font_suffix = _load_font(brand.font_quote, suffix_size)
        lines = _wrap_text_by_pixels(draw, base, font_main, max_width)
        for i, line in enumerate(lines):
            if y + lh_main > max_y:
                break
            draw.text((x, y), line, font=font_main, fill=fill)
            if i == len(lines) - 1 and line:
                bbox = draw.textbbox((x, y), line, font=font_main)
                suffix_x = bbox[2] + 6
                suffix_y = y + max(0, (lh_main - suffix_size) // 2)
                if suffix_x + 40 < x + max_width:
                    draw.text((suffix_x, suffix_y), suffix, font=font_suffix, fill=fill)
            y += lh_main
        return y

    font_topic = _load_font(brand.font_heading, main_size)
    return _draw_wrapped_text(
        draw=draw,
        text=topic,
        x=x,
        y=y,
        font=font_topic,
        fill=fill,
        max_width=max_width,
        line_height=lh_main,
        max_y=max_y,
        canvas_width=brand.canvas_width,
    )


# ── Page footer label ─────────────────────────────────────────────────────────

def _make_page_label(
    page: PageContent,
    brand: BrandSpec,
    *,
    sent_total: int | None = None,
) -> str:
    """
    Build the footer page label.

    messenger_card:  "Bản luận giải · 2/5"
    a4:              "trang 2 / 5"

    sent_total: if provided, use this as the denominator (actual cards sent)
                rather than page.total_pages (paginator count). This ensures
                footer shows "1/3" when only 3 cards are delivered even if
                the paginator produced more.
    """
    total = sent_total if sent_total is not None else page.total_pages
    if brand.render_profile == "messenger_card":
        return f"Bản luận giải · {page.page_num}/{total}"
    return f"trang {page.page_num} / {total}"


# ── Single-page renderer ──────────────────────────────────────────────────────

def render_page(
    page: PageContent,
    brand: BrandSpec = DEFAULT_BRAND,
    *,
    mode: str = "palm",
    show_page_number: bool = True,
    template_variant: str = "default",
    user_focus: str | None = None,
    sent_total: int | None = None,
) -> bytes:
    """
    Render a single PageContent into JPEG bytes.

    Handles both "a4" and "messenger_card" render profiles via BrandSpec.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except ImportError:
        raise RuntimeError("Pillow not installed — add Pillow to requirements.txt")

    from PIL import ImageDraw

    from app.render.layout import fit_layout_for_page

    cleaned_sections = [
        {
            "heading": _clean_markdown_for_render(s["heading"]) if s.get("heading") else None,
            "body": _clean_markdown_for_render(s.get("body", "")),
        }
        for s in page.sections
    ]
    layout_plan = fit_layout_for_page(
        page,
        brand,
        user_focus=user_focus,
        cleaned_sections=cleaned_sections,
    )

    canvas = _make_canvas(brand, template_variant=template_variant)

    # Draw semi-transparent text card overlay for messenger_card
    if brand.render_profile == "messenger_card":
        _draw_text_card_overlay(canvas, brand, card_bottom_y=layout_plan.card_bottom_y)

    draw = ImageDraw.Draw(canvas)

    gold_rgb = _hex_to_rgb(brand.color_accent_gold)
    body_color = _hex_to_rgb(brand.color_body)
    heading_color = _hex_to_rgb(brand.color_heading)
    pink_rgb = _hex_to_rgb(brand.color_accent_pink)

    font_heading = _load_font(brand.font_heading, layout_plan.heading_font_size)
    font_body = _load_font(brand.font_body, layout_plan.body_font_size)
    font_quote = _load_font(brand.font_quote, brand.font_size_quote)

    # 1. Logo
    _draw_logo(canvas, brand)

    # 2. Mode label header — centered in header zone
    mode_label = _MODE_LABELS.get(mode, mode.title())
    if brand.render_profile == "messenger_card":
        # Right-aligned short label next to logo
        label_x = brand.canvas_width - brand.safe_area_x
        label_y = max(brand.safe_area_y_start - 48, 24)
        label_font = _load_font(brand.font_body, max(20, brand.font_size_body - 10))
        draw.text(
            (label_x, label_y),
            mode_label,
            font=label_font,
            fill=_hex_to_rgb(brand.color_accent_gold),
            anchor="rt",
        )
    else:
        label_y = max(brand.safe_area_y_start - 110, 60)
        draw.text(
            (brand.canvas_width // 2, label_y),
            mode_label,
            font=font_heading,
            fill=heading_color,
            anchor="mt",
        )

    # 3. Decorative gold divider
    divider_y = brand.safe_area_y_start - 12
    draw.rectangle(
        [brand.safe_area_x, divider_y,
         brand.canvas_width - brand.safe_area_x, divider_y + _ACCENT_LINE_HEIGHT_PX],
        fill=gold_rgb,
    )

    line_height_body = int(layout_plan.body_font_size * layout_plan.line_height_ratio)
    line_height_heading = int(layout_plan.heading_font_size * layout_plan.line_height_ratio)
    line_height_quote = int(brand.font_size_quote * layout_plan.line_height_ratio)
    max_text_width = brand.safe_area_width
    x_start = brand.safe_area_x

    section_gap = layout_plan.section_gap if brand.render_profile == "messenger_card" else 24

    # 4. Card topic title (messenger_card only) — replaces old italic intro excerpt
    #    Topic is derived from page heading/section metadata, never from body text.
    y_cursor = brand.safe_area_y_start + 14 + layout_plan.content_y_offset
    if brand.render_profile == "messenger_card":
        topic = _derive_card_topic(page, user_focus=user_focus, page_index=page.page_num - 1)
        # For continuation pages, append " · tiếp theo" to the topic
        if page.is_continuation:
            topic = f"{topic} · tiếp theo"
        # Only render topic title if it differs from the first section heading
        # (avoids showing heading twice: once as topic title, once as section heading below)
        first_heading = None
        if page.sections:
            first_heading = page.sections[0].get("heading")
        show_topic_title = True
        if (
            not page.is_continuation
            and first_heading
            and _clean_heading_text(first_heading).lower() == topic.lower()
        ):
            show_topic_title = False  # section heading already covers this
        if show_topic_title and topic != "Bản luận giải" and not re.fullmatch(
            r"\d+\.?", topic.strip()
        ):
            y_cursor = _draw_messenger_topic_title(
                draw=draw,
                topic=topic,
                page=page,
                brand=brand,
                layout_plan=layout_plan,
                x=x_start,
                y=y_cursor,
                fill=heading_color,
                max_width=max_text_width,
                max_y=brand.safe_area_y_end - 80,
            )
            y_cursor += section_gap

    # 5. Body sections — use pre-cleaned text from layout plan
    for section, cleaned in zip(page.sections, cleaned_sections):
        heading = cleaned.get("heading")
        body = cleaned.get("body", "")

        if y_cursor + line_height_heading > brand.safe_area_y_end - 60:
            break

        if heading:
            # Pink accent bar + heading text
            draw.rectangle(
                [x_start, y_cursor, x_start + 64, y_cursor + 4],
                fill=pink_rgb,
            )
            y_cursor += 10

            y_cursor = _draw_wrapped_text(
                draw=draw,
                text=heading,
                x=x_start,
                y=y_cursor,
                font=font_heading,
                fill=heading_color,
                max_width=max_text_width,
                line_height=line_height_heading,
                max_y=brand.safe_area_y_end - 60,
                canvas_width=brand.canvas_width,
            )
            y_cursor += 10

        if body:
            y_cursor = _draw_wrapped_text(
                draw=draw,
                text=body,
                x=x_start,
                y=y_cursor,
                font=font_body,
                fill=body_color,
                max_width=max_text_width,
                line_height=line_height_body,
                max_y=brand.safe_area_y_end - 60,
                canvas_width=brand.canvas_width,
            )
            y_cursor += section_gap

    # 5b. Overflow guard — warn if content exceeded safe area
    content_height_used = y_cursor - brand.safe_area_y_start - layout_plan.content_y_offset
    content_height_max = brand.safe_area_y_end - brand.safe_area_y_start
    fill_ratio = content_height_used / max(content_height_max, 1)
    if fill_ratio > 1.05:
        logger.warning(
            "renderer_overflow page=%d fill_ratio=%.2f profile=%s body_px=%d lh=%.2f event=renderer_overflow",
            page.page_num,
            fill_ratio,
            brand.render_profile,
            layout_plan.body_font_size,
            layout_plan.line_height_ratio,
        )

    # 6. Footer divider + page label
    footer_top = brand.safe_area_y_end + 8
    draw.rectangle(
        [brand.safe_area_x, footer_top,
         brand.canvas_width - brand.safe_area_x, footer_top + _ACCENT_LINE_HEIGHT_PX],
        fill=gold_rgb,
    )

    if show_page_number and page.total_pages > 1:
        footer_font_size = max(20, brand.font_size_body - 10)
        font_footer = _load_font(brand.font_body, footer_font_size)
        page_text = _make_page_label(page, brand, sent_total=sent_total)
        draw.text(
            (brand.canvas_width // 2, footer_top + 14),
            page_text,
            font=font_footer,
            fill=_hex_to_rgb(brand.color_accent_gold),
            anchor="mt",
        )

    _draw_commercial_footer(draw, brand, footer_top + _ACCENT_LINE_HEIGHT_PX + 48)
    _draw_card_referral_line(draw, brand)

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=brand.jpeg_quality, optimize=True)
    return buf.getvalue()


# ── Multi-page renderer ───────────────────────────────────────────────────────

def render_all_pages(
    sections: dict[str, str],
    brand: BrandSpec = DEFAULT_BRAND,
    *,
    mode: str = "palm",
    template_variant: str = "default",
    user_focus: str | None = None,
    sent_total: int | None = None,
) -> list[bytes]:
    """
    Paginate + render all pages for an analysis result.

    Args:
        sections: {heading_slug: body_text} from analyzer._parse_sections()
        brand: BrandSpec — use MESSENGER_CARD_BRAND for Messenger sends
        mode: analysis mode
        template_variant: "default" or "alt"
        user_focus: optional user intent string used to derive card topics
        sent_total: if set, overrides the total-pages denominator in footers
                    (use when only a subset of pages are delivered to Messenger)

    Returns:
        list of JPEG bytes, one per page.
    """
    from app.render.paginator import paginate_analysis
    pages = paginate_analysis(sections, brand)
    logger.info(
        "render_pages count=%d mode=%s variant=%s profile=%s",
        len(pages), mode, template_variant, brand.render_profile,
    )

    result: list[bytes] = []
    for page in pages:
        jpeg_bytes = render_page(
            page,
            brand,
            mode=mode,
            show_page_number=True,
            template_variant=template_variant,
            user_focus=user_focus,
            sent_total=sent_total,
        )
        result.append(jpeg_bytes)
        logger.debug(
            "page_rendered page=%d/%d size_kb=%.1f profile=%s",
            page.page_num, page.total_pages, len(jpeg_bytes) / 1024,
            brand.render_profile,
        )

    return result


def pick_template_variant(seed: str) -> str:
    """Hash-based template selection across `default`/`alt` (V9 §8.4)."""
    if not seed:
        return "default"
    import hashlib
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return "alt" if h % 2 == 1 else "default"


# ── CTA Card ("Tri Âm tiếp nối") — LEGACY: disconnected from production delivery.
# Active support CTA is text-only via app.services.support_cta_service.
#
# Env vars (priority: MESSENGER_CTA_* > MESSENGER_FOOTER_* legacy):
#
#   MESSENGER_CTA_ENABLED            "true" | "false"  (default true)
#   MESSENGER_CTA_UPGRADE_LINE       short text label for upgrade notice
#   MESSENGER_CTA_AFFILIATE_URL      https:// Shopee affiliate URL
#   MESSENGER_CTA_AFFILIATE_DISPLAY_LABEL  short label shown instead of raw URL
#   MESSENGER_CTA_ALLOWED_DOMAINS    comma-separated whitelist (e.g. "s.shopee.vn")
#   MESSENGER_CTA_BANK_BLOCK         multi-line bank info (\\n = newline)
#   MESSENGER_FOOTER_BANK_BLOCK      legacy fallback for bank block
#   MESSENGER_FOOTER_AFFILIATE_LINE  legacy fallback for affiliate label

def _is_safe_cta_url(url: str, allowed_domains: list[str]) -> bool:
    """
    Return True only if url is safe to display on the CTA card.

    Rules:
      - Must start with https://
      - Must not start with http://, javascript:, data:, ftp:
      - If allowed_domains is non-empty, netloc must be one of those domains
        (or a subdomain of one).
    Never raises.
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.scheme != "https":
            return False
        if not p.netloc:
            return False
        if allowed_domains:
            netloc = p.netloc.lower().split(":")[0]  # strip port
            for d in allowed_domains:
                d = d.strip().lower()
                if netloc == d or netloc.endswith("." + d):
                    return True
            return False
        return True
    except Exception:
        return False


def _read_cta_config() -> dict:
    """Read all CTA env vars and return a unified config dict."""
    def _env(key: str, default: str = "") -> str:
        return (os.environ.get(key) or "").strip() or default

    enabled_raw = _env("MESSENGER_CTA_ENABLED", "true").lower()
    enabled = enabled_raw not in ("false", "0", "no", "off")

    allowed_raw = _env("MESSENGER_CTA_ALLOWED_DOMAINS")
    allowed_domains = [d.strip() for d in allowed_raw.split(",") if d.strip()]

    affiliate_url = _env("MESSENGER_CTA_AFFILIATE_URL")
    affiliate_label = _env("MESSENGER_CTA_AFFILIATE_DISPLAY_LABEL")

    # Legacy fallback for affiliate label
    if not affiliate_label:
        legacy_aff = _env("MESSENGER_FOOTER_AFFILIATE_LINE")
        if legacy_aff:
            affiliate_label = legacy_aff

    # Validate URL
    if affiliate_url and not _is_safe_cta_url(affiliate_url, allowed_domains):
        logger.warning("cta_affiliate_url_unsafe url=%s — skipping", affiliate_url[:80])
        affiliate_url = ""

    # Displayed label: prefer explicit label, fall back to domain, then blank
    if affiliate_url and not affiliate_label:
        try:
            from urllib.parse import urlparse
            affiliate_label = urlparse(affiliate_url).netloc
        except Exception:
            affiliate_label = ""

    # Bank block — MESSENGER_CTA_BANK_BLOCK > legacy MESSENGER_FOOTER_BANK_BLOCK
    bank_block = _env("MESSENGER_CTA_BANK_BLOCK") or _env("MESSENGER_FOOTER_BANK_BLOCK")
    bank_block = bank_block.replace("\\n", "\n")

    upgrade_line = _env("MESSENGER_CTA_UPGRADE_LINE")

    return {
        "enabled": enabled,
        "upgrade_line": upgrade_line,
        "affiliate_url": affiliate_url,
        "affiliate_label": affiliate_label,
        "bank_block": bank_block,
        "allowed_domains": allowed_domains,
    }


def _build_cta_text_blocks(cfg: dict) -> list[dict]:
    """
    Build ordered list of text blocks for the CTA card.

    Each block: {"label": str | None, "lines": list[str], "style": "heading"|"body"|"small"}

    Order: upgrade → affiliate → safety → donate
    A block is included only if it has actual content.
    """
    blocks: list[dict] = []

    # ── 1. Mời đi tiếp ──────────────────────────────────────────────────────
    from app.services.feature_flags import is_multimodal_enabled

    if is_multimodal_enabled():
        upgrade_body = (
            "Nếu phần luận giải này hữu ích với bạn, Demo Bot sẽ tiếp tục"
            " mở thêm các trải nghiệm sâu hơn như xem tướng, chỉ tay và bản luận giải"
            " cá nhân hoá hơn."
        )
    else:
        upgrade_body = (
            "Nếu phần luận giải này hữu ích với bạn, Demo Bot sẽ tiếp tục"
            " mở thêm các bản luận giải tử vi sâu hơn và cá nhân hoá hơn."
        )
    upgrade_lines = [
        "🌿  Tri Âm tiếp nối",
        "",
        upgrade_body,
        "",
        "Bạn có thể theo dõi để nhận bản nâng cấp khi sẵn sàng.",
    ]
    if cfg["upgrade_line"]:
        upgrade_lines += ["", f"Nhận thông báo: {cfg['upgrade_line']}"]
    blocks.append({"label": None, "lines": upgrade_lines, "style": "body"})

    # ── 2. Affiliate Shopee minh bạch ────────────────────────────────────────
    if cfg["affiliate_url"] or cfg["affiliate_label"]:
        aff_lines = [
            "🛍️  Ủng hộ qua mua sắm",
            "",
            "Khi cần mua sắm trên Shopee, bạn có thể mở link giới thiệu của"
            " Demo Bot trước rồi mua như bình thường.",
            "",
            "Bạn không trả thêm phí. Nếu đơn hàng được ghi nhận, team có thể"
            " nhận một khoản hoa hồng nhỏ từ nền tảng để duy trì hệ thống.",
        ]
        # Link is delivered as a clickable Messenger button — not repeated here as plain text
        aff_lines += ["", "→ Nhấn nút bên dưới để mở link (không tốn phí thêm)."]
        blocks.append({"label": None, "lines": aff_lines, "style": "body"})

        # Safety note – always shown when affiliate present
        safety_lines = [
            "Lưu ý an toàn:",
            "• Demo Bot không yêu cầu OTP, mật khẩu hay cài app lạ.",
            "• Chỉ mở link được gửi trong cuộc trò chuyện chính thức này.",
            "• Nếu thấy link bất thường, bạn có thể bỏ qua hoàn toàn.",
        ]
        blocks.append({"label": None, "lines": safety_lines, "style": "small"})

    # ── 3. Donate – cuối, nhỏ, tùy tâm ─────────────────────────────────────
    if cfg["bank_block"]:
        donate_lines = [
            "💛  Ủng hộ tùy tâm",
            "",
            "Demo Bot vận hành nhờ sự đồng hành của những người bạn đã ủng hộ."
            " Nếu buổi luận giải hôm nay hữu ích, một ly cà phê nhỏ"
            " sẽ giúp team có thêm động lực để tiếp tục lâu dài, ủng hộ tại:",
            "",
        ]
        for ln in cfg["bank_block"].split("\n"):
            donate_lines.append(ln.strip())
        donate_lines += [
            "",
            "Tuỳ tâm — không có cũng không sao, Tri Âm vẫn luôn ở đây lắng nghe bạn.",
        ]
        blocks.append({"label": None, "lines": donate_lines, "style": "body"})

    return blocks


def render_cta_card(
    brand: BrandSpec | None = None,
    *,
    template_variant: str = "default",
) -> bytes | None:
    """
    Render the "Tri Âm tiếp nối" CTA card as a JPEG.

    Returns None when:
      - MESSENGER_CTA_ENABLED=false
      - No upgrade / affiliate / bank config at all
      - Pillow not installed (silent — caller handles fallback)

    Never raises. All failures are logged as warnings.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except ImportError:
        logger.warning("render_cta_card: Pillow not installed — skipping")
        return None

    from PIL import ImageDraw
    from app.render.branding import MESSENGER_CARD_BRAND

    if brand is None:
        brand = MESSENGER_CARD_BRAND

    try:
        cfg = _read_cta_config()

        if not cfg["enabled"]:
            logger.debug("render_cta_card: disabled via MESSENGER_CTA_ENABLED=false")
            return None

        has_content = any([cfg["upgrade_line"], cfg["affiliate_url"],
                           cfg["affiliate_label"], cfg["bank_block"]])
        if not has_content:
            logger.debug("render_cta_card: no CTA env vars set — skipping")
            return None

        blocks = _build_cta_text_blocks(cfg)
        if not blocks:
            return None

        # ── Canvas + overlay ────────────────────────────────────────────────
        canvas = _make_canvas(brand, template_variant=template_variant)
        _draw_text_card_overlay(canvas, brand)
        draw = ImageDraw.Draw(canvas)

        gold_rgb = _hex_to_rgb(brand.color_accent_gold)
        body_color = _hex_to_rgb(brand.color_body)
        heading_color = _hex_to_rgb(brand.color_heading)

        font_heading = _load_font(brand.font_heading, brand.font_size_heading)
        font_body = _load_font(brand.font_body, brand.font_size_body)
        font_small = _load_font(brand.font_body, max(20, brand.font_size_body - 8))

        # ── Logo ─────────────────────────────────────────────────────────────
        _draw_logo(canvas, brand)

        # ── Header label ─────────────────────────────────────────────────────
        label_x = brand.canvas_width - brand.safe_area_x
        label_y = max(brand.safe_area_y_start - 48, 24)
        label_font = _load_font(brand.font_body, max(20, brand.font_size_body - 10))
        draw.text(
            (label_x, label_y),
            "Tri Âm tiếp nối",
            font=label_font,
            fill=gold_rgb,
            anchor="rt",
        )

        # ── Gold divider ──────────────────────────────────────────────────────
        divider_y = brand.safe_area_y_start - 12
        draw.rectangle(
            [brand.safe_area_x, divider_y,
             brand.canvas_width - brand.safe_area_x, divider_y + _ACCENT_LINE_HEIGHT_PX],
            fill=gold_rgb,
        )

        # ── Text blocks ───────────────────────────────────────────────────────
        x = brand.safe_area_x
        y = brand.safe_area_y_start + 14
        max_y = brand.safe_area_y_end - 40
        max_w = brand.safe_area_width
        line_h_body = int(brand.font_size_body * brand.line_height_ratio)
        line_h_small = int(max(20, brand.font_size_body - 8) * 1.40)
        block_gap = 28

        for block in blocks:
            font = font_small if block["style"] == "small" else font_body
            lh = line_h_small if block["style"] == "small" else line_h_body
            clr = _hex_to_rgb(brand.color_accent_gold) if block["style"] == "small" else body_color

            for raw_line in block["lines"]:
                if y >= max_y:
                    break
                if not raw_line.strip():
                    y += lh // 2
                    continue
                wrapped = _wrap_text_by_pixels(draw, raw_line, font, max_w)
                for wl in wrapped:
                    if y + lh > max_y:
                        break
                    if wl.strip():
                        draw.text((x, y), wl, font=font, fill=clr)
                    y += lh

            y += block_gap

        # ── Footer divider + closing line ─────────────────────────────────────
        footer_top = brand.safe_area_y_end + 8
        draw.rectangle(
            [brand.safe_area_x, footer_top,
             brand.canvas_width - brand.safe_area_x,
             footer_top + _ACCENT_LINE_HEIGHT_PX],
            fill=gold_rgb,
        )
        footer_font = _load_font(brand.font_quote, max(22, brand.font_size_body - 8))
        draw.text(
            (brand.canvas_width // 2, footer_top + 16),
            "Cảm ơn bạn đã tin Demo Bot 🌿",
            font=footer_font,
            fill=gold_rgb,
            anchor="mt",
        )

        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=brand.jpeg_quality, optimize=True)
        logger.info(
            "render_cta_card done size_kb=%.1f has_affiliate=%s has_bank=%s",
            buf.tell() / 1024,
            bool(cfg["affiliate_url"]),
            bool(cfg["bank_block"]),
        )
        return buf.getvalue()

    except Exception as exc:
        logger.warning("render_cta_card failed err=%s — skipping", exc)
        return None
