"""DataTalks.Club Weekly email layout: the issue #302 Mailchimp HTML, filled from blocks.

``mailing/newsletter_layout/dtc_weekly.html`` is the HTML of issue #302 as it
was sent on 2026-11-09 (Relay campaign 18, exported from Mailchimp), with the
issue-specific parts cut out and replaced by markers:

- ``<!--RELAY:TITLE-->`` and ``<!--RELAY:PREHEADER-->``: subject and inbox preview;
- ``<!--RELAY:BANNER-->``: the text block above the logo (test banner), before
  "View this email in your browser" (``<!--RELAY:WEB_VIEW_URL-->``);
- ``<!--RELAY:INTRO-->``: the issue label and "This week" text under the logo;
- ``<!--RELAY:SECTIONS-->``: every block after the intro.

- ``<!--RELAY:FORWARD_URL-->``: "Forward to a friend" in the footer.

Head/CSS, logo, footer (community text, links, social icons, address) are kept
as #301/#302 had them. Subscribe and Past issues go to the Mailchimp signup form
and campaign archive, like #301. "You can update your preferences or
unsubscribe" keeps Mailchimp's ``list-manage.com/profile|unsubscribe ... e=[UNIQID]``
links: a real campaign send rewrites them to the recipient's Relay unsubscribe
page (``campaign_rendering``), the way the 8 November copy of #302 went out. Blocks are emitted with the same Mailchimp building pieces #302
used (text, image, button and divider blocks inside ``mceWrapper`` sections),
so a newsletter made from blocks looks like the ones we sent before.
"""

import re
from functools import lru_cache
from html import escape
from pathlib import Path

import markdown as markdown_lib

SKELETON_PATH = Path(__file__).resolve().parent.parent / "newsletter_layout" / "dtc_weekly.html"

BRAND_BLUE = "#2056A2"
LINK_BLUE = "rgb(0, 106, 177)"
PROMO_BACKGROUND = "#ECECFB"
FONT = "'Helvetica Neue', Helvetica, Arial, Verdana, sans-serif"

# Blocks drawn as the lavender card #302 used for the course promo.
PROMO_TYPES = {"sponsor", "sponsor_secondary", "course_promo", "own_promo"}
# Course cards: course banner first, then a smaller (h3) title — like the three course cards in #302.
COURSE_TYPES = {"course_update"}

@lru_cache(maxsize=1)
def skeleton():
    return SKELETON_PATH.read_text(encoding="utf-8")


def head_html(skeleton_html=None):
    """``<head>`` of the skeleton (all CSS), for standalone block previews."""
    html = skeleton_html if skeleton_html is not None else skeleton()
    return html[:html.index("<body")]


# --------------------------------------------------------------------------- text

_SAFE_HREF = re.compile(r'href="(?!https?://|mailto:)[^"]*"')
_TOP_LEVEL = re.compile(r"(<p>.*?</p>|<ul>.*?</ul>|<ol>.*?</ol>)", re.S)


def markdown_blocks(text):
    """Operator Markdown -> list of top-level Mailchimp text elements (raw HTML is escaped)."""
    if not (text or "").strip():
        return []
    html = markdown_lib.markdown(escape(text, quote=False), extensions=["sane_lists", "nl2br"])
    html = _SAFE_HREF.sub('href="#"', html)
    html = html.replace("<br />", "<br/>")
    html = re.sub(r'<a href="([^"]*)">', r'<a href="\1" target="_blank">', html)
    html = re.sub(r"<li>(.*?)</li>", r"<li><p>\1</p></li>", html, flags=re.S)
    return [part.replace("\n", "") for part in _TOP_LEVEL.findall(html)]


def join_text(parts):
    """Join elements the way #302's editor did: an empty line between paragraphs,
    none before a list; the last element gets ``class="last-child"``."""
    parts = [part for part in parts if part]
    if not parts:
        return ""
    out = []
    for index, part in enumerate(parts):
        if index and not part.startswith(("<ul", "<ol")):
            out.append("<p><br/></p>")
        out.append(part)
    last = out[-1]
    out[-1] = re.sub(r"^<(p|ul|ol|h2|h3)(?=[ >])", r'<\1 class="last-child"', last, count=1)
    return "".join(out)


def kicker_html(text):
    return ('<p style="letter-spacing: 1.5px;"><strong><span style="color:rgb(32, 86, 162);">'
            '<span style="font-size: 13px"><span style="font-family: Helvetica, Arial, sans-serif">'
            f'{escape(text)}</span></span></span></strong></p>')


def heading_html(text, level="h2", url=""):
    inner = escape(text)
    if url:
        inner = f'<a href="{escape(url)}" target="_blank" style="color:rgb(34, 34, 34);text-decoration:none">{inner}</a>'
    return f"<{level}>{inner}</{level}>"


# --------------------------------------------------------------------------- Mailchimp building pieces

def text_row(inner, *, padding_top="12px"):
    if not inner:
        return ""
    return ('<tr><td style="padding-top:0;padding-bottom:0;padding-right:0;padding-left:0" valign="top" '
            'class="mceGutterContainer"><table border="0" cellpadding="0" cellspacing="0" width="100%" '
            'style="border-collapse:separate" role="presentation"><tbody><tr><td style="padding-top:0;'
            'padding-bottom:0;padding-right:0;padding-left:0;border:0;border-radius:0" valign="top">'
            '<table width="100%" style="border:0;background-color:transparent;border-radius:0;'
            'border-collapse:separate"><tbody><tr><td style="padding-left:24px;padding-right:24px;'
            f'padding-top:{padding_top};padding-bottom:12px" class="mceTextBlockContainer">'
            f'<div class="mceText" style="width:100%">{inner}</div></td></tr></tbody></table>'
            '</td></tr></tbody></table></td></tr>')


def image_row(url, link="", alt=""):
    if not url:
        return ""
    src, alt = escape(url), escape(alt)
    img = (f'<img alt="{alt}" src="{src}" width="612" height="auto" '
           'style="display:block;max-width:100%;height:auto;border-radius:0" class="mceImage"/>')
    table = ('<table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" '
             'style="border-collapse:separate;margin:0;vertical-align:top;max-width:100%;width:100%;height:auto" '
             f'role="presentation"><tbody><tr><td style="border:0;border-radius:0;margin:0" valign="top">{img}'
             '</td></tr></tbody></table>')
    mso_img = (f'<img role="presentation" class="mceImage" src="{src}" alt="{alt}" width="612" height="auto" '
               'style="display:block;max-width:612px;width:612px;height:auto"/>')
    mso_img = ('<span class="mceImageBorder" style="border:0;border-width:2px;vertical-align:top;margin:0">'
               f'{mso_img}</span>')
    if link:
        href = escape(link)
        table = f'<a href="{href}" style="display:block" target="_blank">{table}</a>'
        mso_img = f'<a href="{href}">{mso_img}</a>'
    return ('<tr><td style="background-color:transparent;padding-top:12px;padding-bottom:12px;padding-right:24px;'
            'padding-left:24px;border:0;border-radius:0" valign="top" class="mceImageBlockContainer" align="center">'
            f'<div><!--[if !mso]><!--></div>{table}<div><!--<![endif]--></div><div>\n<!--[if mso]>\n{mso_img}\n'
            '<![endif]-->\n</div></td></tr>')


def button_row(text, url, *, rules=None):
    if not (text and url):
        return ""
    r = resolve_rules(rules)
    brand_blue = r["brand_blue"]
    font = r["font"]
    label, href = escape(text), escape(url)
    width = round(56 + 9.3 * len(text), 2)  # VML needs a fixed width; Mailchimp sized it from the label.
    return ('<tr><td style="background-color:transparent;padding-top:12px;padding-bottom:12px;padding-right:24px;'
            'padding-left:24px;border:0;border-radius:0" valign="top" class="mceButtonBlockContainer" align="center">'
            '<div><!--[if !mso]><!--></div><table align="center" border="0" cellpadding="0" cellspacing="0" '
            'role="presentation" class="mceButtonContainer"><tbody><tr class="mceStandardButton">'
            f'<td style="background-color:{brand_blue};border-radius:8px;text-align:center" valign="top" '
            f'class="mceButton"><a href="{href}" target="_blank" class="mceButtonLink" style="background-color:'
            f'{brand_blue};border-radius:8px;border:2px none #000000;color:#ffffff;display:block;font-family:{font};'
            'font-size:16px;font-weight:bold;font-style:normal;padding:16px 28px;text-decoration:none;'
            f'text-align:center;direction:ltr;letter-spacing:0px" rel="noreferrer">{label}</a></td></tr></tbody>'
            '</table><div><!--<![endif]--></div><table align="center" border="0" cellpadding="0" cellspacing="0" '
            'role="presentation" class="mceButtonContainer"><tbody><tr>\n<!--[if mso]>\n<td align="center">\n'
            '<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml"\nxmlns:w="urn:schemas-microsoft-com:office:word"\n'
            f'href="{href}"\nstyle="v-text-anchor:middle; width:{width}px; height:51.5px;"\narcsize="5%"\n'
            f'strokecolor="{brand_blue}"\nstrokeweight="1px"\nfillcolor="{brand_blue}">\n'
            '<v:stroke dashstyle="solid"/>\n<w:anchorlock />\n<center style="\ncolor: #ffffff;\ndisplay: block;\n'
            f"font-family: {font};\nfont-size: 16;\nfont-style: normal;\nfont-weight: bold;\nletter-spacing: 0px;\n"
            f'text-decoration: none;\ntext-align: center;\ndirection: ltr;"\n>\n{label}\n</center>\n</v:roundrect>\n'
            '</td>\n<![endif]-->\n</tr></tbody></table></td></tr>')


DIVIDER_ROW = (
    '<tr><td style="background-color:transparent;padding-top:12px;padding-bottom:12px;padding-right:24px;'
    'padding-left:24px;border:0;border-radius:0" valign="top" class="mceDividerBlockContainer">'
    '<table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" '
    'style="background-color:transparent;width:100%" role="presentation" class="mceDividerContainer"><tbody><tr>'
    '<td style="min-width:100%;border-top-width:1px;border-top-style:solid;border-top-color:#E5E5E5;line-height:0;'
    'font-size:0" valign="top" class="mceDividerBlock"> </td></tr></tbody></table></td></tr>'
)


# #302 put each following course card in a layout block with 8px extra padding, not a divider.
COURSE_GAP_ROW = '<tr><td style="height:8px;line-height:8px;font-size:0" valign="top">&nbsp;</td></tr>'


DEFAULT_LAYOUT_RULES = {
    "brand_blue": BRAND_BLUE,
    "link_blue": LINK_BLUE,
    "promo_background": PROMO_BACKGROUND,
    "font": FONT,
    "promo_types": sorted(PROMO_TYPES),
    "course_types": sorted(COURSE_TYPES),
    "close_adjacent_promos": True,
    "section_class": "mceSection54",
    "promo_section_class": "mceSection46",
    "divider_html": DIVIDER_ROW,
    "course_gap_html": COURSE_GAP_ROW,
    "closing_divider": True,
}


def resolve_rules(rules=None):
    """Merge template-stored ``layout_rules`` with ``DEFAULT_LAYOUT_RULES``."""
    merged = dict(DEFAULT_LAYOUT_RULES)
    if isinstance(rules, dict):
        for key, value in rules.items():
            if value is not None:
                merged[key] = value
    merged["promo_types"] = set(merged.get("promo_types") or ())
    merged["course_types"] = set(merged.get("course_types") or ())
    return merged


def section(rows, *, background="#FFFFFF", css_class="mceSection54"):
    """One ``mceWrapper`` section, as #302 wrapped each group of blocks.

    ``mceSection54`` and ``mceSection46`` are #302's own section classes, so the
    skeleton's CSS styles them exactly as before (46 is the lavender promo card).
    """
    return (
        f'<tbody class="mceWrapper"><tr><td style="background-color:transparent" valign="top" align="center" '
        f'class="{css_class}"><!--[if (gte mso 9)|(IE)]><table align="center" border="0" cellspacing="0" '
        'cellpadding="0" width="660" style="width:660px;"><tr><td><![endif]--><table border="0" cellpadding="0" '
        'cellspacing="0" width="100%" style="max-width:660px" role="presentation"><tbody><tr>'
        f'<td style="background-color:{background}" valign="top" class="mceWrapperInner"><table align="center" '
        'border="0" cellpadding="0" cellspacing="0" width="100%" role="presentation"><tbody><tr class="mceRow">'
        '<td style="background-position:center;background-repeat:no-repeat;background-size:cover" valign="top">'
        '<table border="0" cellpadding="0" cellspacing="0" width="100%" role="presentation"><tbody><tr>'
        '<td style="padding-top:0;padding-bottom:0" valign="top" class="mceColumn" colspan="12" width="100%">'
        f'<table border="0" cellpadding="0" cellspacing="0" width="100%" role="presentation"><tbody>{rows}'
        '</tbody></table></td></tr></tbody></table></td></tr></tbody></table></td></tr></tbody></table>'
        '<!--[if (gte mso 9)|(IE)]></td></tr></table><![endif]--></td></tr></tbody>'
    )


# --------------------------------------------------------------------------- blocks

def _lines(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _events_text(events, footer, valid_url, *, rules=None):
    r = resolve_rules(rules)
    link_blue, brand_blue = r["link_blue"], r["brand_blue"]
    spacer = '<p style="line-height: 0; mso-line-height-alt: 0%;"><br/></p>'
    out = [spacer]
    for line in _lines(events):
        parts = [part.strip() for part in line.split("|")]
        when, title = parts[0], parts[1] if len(parts) > 1 else ""
        url = parts[2] if len(parts) > 2 and valid_url(parts[2]) else ""
        host = f" {escape(parts[3])}" if len(parts) > 3 and parts[3] else ""
        out.append(f"<p><strong>{escape(when)}</strong></p>")
        if title:
            title_html = (f'<a href="{escape(url)}" style="color: {link_blue};">{escape(title)}</a>' if url
                          else escape(title))
            out.append(f"<p>{title_html}{host}</p>")
        out.append("<p><br/></p>")
    footer_parts = markdown_blocks(footer)
    if footer_parts:
        out.append(spacer)
        out.extend(re.sub(r'target="_blank">', f'target="_blank" style="color: {brand_blue};">', part)
                   for part in footer_parts)
    out[-1] = re.sub(r"^<p(?=[ >])", '<p class="last-child"', out[-1], count=1)
    return "".join(out)


def _links_text(links, valid_url):
    items = []
    for line in _lines(links):
        parts = [part.strip() for part in line.split("|")]
        url = parts[1] if len(parts) > 1 and valid_url(parts[1]) else ""
        note = f" — {escape(parts[2])}" if len(parts) > 2 and parts[2] else ""
        title = f'<a href="{escape(url)}" target="_blank">{escape(parts[0])}</a>' if url else escape(parts[0])
        items.append(f"<li><p>{title}{note}</p></li>")
    return f'<ul class="last-child">{"".join(items)}</ul>' if items else ""


def block_rows(block_type, spec, get, valid_url, *, rules=None):
    """Mailchimp rows for one block (without the section wrapper); "" when the block is empty."""
    r = resolve_rules(rules)
    render = spec["render"]
    if render == "html":
        return text_row(get("html"))
    if render == "intro":
        return intro_row(get("issue_label"), get("summary"))
    kicker = text_row(join_text([kicker_html(get("kicker"))])) if get("kicker") else ""
    if render == "events":
        events = _events_text(get("events"), get("footer"), valid_url, rules=r) if (get("events") or get("footer")) else ""
        return kicker + text_row(join_text([heading_html(get("title"))]) if get("title") else "") + text_row(events)
    if render == "links":
        return (kicker + text_row(join_text([heading_html(get("title"))]) if get("title") else "")
                + text_row(_links_text(get("links"), valid_url)))
    title, image_url, image_link = get("title"), get("image_url"), get("image_link")
    body = markdown_blocks(get("body"))
    bullets = _lines(get("bullets"))
    if bullets:
        body.append("<ul>" + "".join(f"<li><p>{escape(item)}</p></li>" for item in bullets) + "</ul>")
    body.extend(markdown_blocks(get("closing")))
    meta = f"<p><strong>{escape(get('meta'))}</strong></p>" if get("meta") else ""
    image = image_row(image_url, image_link, title)
    button = button_row(get("cta_text"), get("cta_url"), rules=r)
    if block_type in r["course_types"]:
        heading = heading_html(title, "h3", image_link if not image_url else "") if title else ""
        return (kicker + image + text_row(join_text([heading]) if heading else "")
                + text_row(join_text([meta, *body])) + button)
    heading = heading_html(title, "h2", image_link if not image_url else "") if title else ""
    return (kicker + text_row(join_text([heading]) if heading else "") + text_row(join_text([meta]) if meta else "")
            + image + text_row(join_text(body)) + button)


def intro_row(label, summary):
    parts = [f"<p>{escape(label)}</p>" if label else "", *markdown_blocks(summary)]
    return text_row(join_text(parts), padding_top="0")


def note_row(html):
    return text_row(html)


def sections_html(items, *, rules=None):
    """``items``: ``[(block_type, rows_html)]`` in order -> the content sections.

    Like #301/#302: promo blocks are lavender sections; every other block is a
    white section that starts with a divider (except right after the intro, whose
    divider is in the skeleton); consecutive course cards share one section with
    no divider between them. A closing divider precedes the footer.

    When ``close_adjacent_promos`` is True (default), only the first of two
    adjacent promo blocks is lavender: #301 put the course promo that followed
    the sponsored card in a white section with a divider, so the sponsored card
    ends visibly.
    """
    r = resolve_rules(rules)
    promo_types = r["promo_types"]
    course_types = r["course_types"]
    close_adjacent_promos = bool(r["close_adjacent_promos"])
    promo_bg = r["promo_background"]
    section_cls = r["section_class"]
    promo_cls = r["promo_section_class"]
    divider_html = r["divider_html"]
    course_gap_html = r["course_gap_html"]
    closing_divider = bool(r["closing_divider"])

    out, current, previous = [], [], "intro"

    def flush():
        if current:
            out.append(section("".join(current), css_class=section_cls))
            current.clear()

    for block_type, rows in items:
        is_promo = block_type in promo_types and (not close_adjacent_promos or previous not in promo_types)
        if is_promo:
            flush()
            out.append(section(rows, background=promo_bg, css_class=promo_cls))
        elif block_type in course_types and previous in course_types and current:
            current.append(course_gap_html + rows)
        else:
            flush()
            current.append(("" if previous == "intro" else divider_html) + rows)
        previous = block_type
    if previous != "intro" and closing_divider:
        current.append(divider_html)
    flush()
    return "".join(out)


def document(*, title, preheader, banner_html, intro_html, sections, web_view_url="#", forward_url="#",
             skeleton_html=None):
    html = skeleton_html if skeleton_html is not None else skeleton()
    # The skeleton keeps #302's invisible filler after the preheader, so mail
    # clients do not pull body text into the inbox preview.
    for marker, value in (("<!--RELAY:TITLE-->", escape(title, quote=False)),
                          ("<!--RELAY:PREHEADER-->", escape(preheader or "", quote=False)),
                          ("<!--RELAY:BANNER-->", banner_html),
                          ("<!--RELAY:WEB_VIEW_URL-->", escape(web_view_url or "#")),
                          ("<!--RELAY:INTRO-->", intro_html), ("<!--RELAY:SECTIONS-->", sections),
                          ("<!--RELAY:FORWARD_URL-->", escape(forward_url or "#"))):
        html = html.replace(marker, value, 1)
    return html


def block_preview_document(block_type, rows, *, rules=None, skeleton_html=None):
    """A single block in the newsletter's CSS, for the editor's per-block preview."""
    if not rows.strip():
        return ""
    r = resolve_rules(rules)
    is_promo = block_type in r["promo_types"]
    background = r["promo_background"] if is_promo else "#FFFFFF"
    css_class = r["promo_section_class"] if is_promo else r["section_class"]
    if block_type == "intro":
        css_class, background = "mceSection4", "#ffffff"
    return (f'{head_html(skeleton_html=skeleton_html)}<body style="margin:0;background:#ffffff">'
            '<table id="root" border="0" cellpadding="0" cellspacing="0" width="100%" role="presentation">'
            f'{section(rows, background=background, css_class=css_class)}</table></body></html>')
