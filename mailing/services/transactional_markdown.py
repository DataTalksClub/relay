"""Markdown-with-frontmatter support for transactional templates.

This is the Datamailer port of the AI Shipping Labs email template format:
a markdown body with a YAML frontmatter block (`subject` required;
`required_context` and `category` optional), rendered with
context-substitution first, then markdown -> sanitized HTML, then wrapped in
the shared HTML email chrome (the port of AISL's
``_render_template_with_footer`` pipeline). Plain text derives from the same
context-rendered markdown source.
"""

import re

import nh3
import yaml
from markdown import markdown as markdown_to_html

FRONTMATTER_DELIMITER = "---"

# nh3 (ammonia) allowlist for sanitizing rendered markdown HTML. It covers
# every element the markdown renderer legitimately emits (headings, lists,
# tables, code/pre, blockquotes, links, images) while stripping anything else
# -- notably <script>/<style> and inline event handlers -- so raw HTML in a
# template body is removed rather than delivered.
_SANITIZE_TAGS = frozenset(
    {
        "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre",
        "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead",
        "tr", "ul",
    }
)
_SANITIZE_ATTRIBUTES = {
    "a": {"href", "title", "target", "rel"},
    "img": {"src", "alt", "title"},
    "span": {"class"},
    "code": {"class"},
    "pre": {"class"},
    "td": {"align"},
    "th": {"align"},
}

# Markdown extensions kept in step with the AISL email renderer: fenced code
# blocks and tables parse; there is no syntax-highlighting or diagram runtime
# in an inbox, so those AISL extensions are intentionally not ported.
MARKDOWN_EXTENSIONS = ["fenced_code", "tables"]

_LINK_RE = re.compile(r"!?\[(?P<label>[^\]]*)\]\((?P<url>[^)]+)\)")
_BOLD_RE = re.compile(r"(?<!\w)(\*\*|__)(?P<text>.+?)\1(?!\w)")
_ITALIC_RE = re.compile(r"(?<!\w)(\*|_)(?P<text>[^*_]+)\1(?!\w)")
_CODE_RE = re.compile(r"`(?P<text>[^`]+)`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")


class TemplateMarkdownError(Exception):
    """Raised when a markdown template source cannot be parsed."""


def parse_frontmatter(source):
    """Split a markdown template into ``(metadata, markdown_body)``.

    The source must start with a ``---`` delimiter line, a YAML block, and a
    closing ``---`` line. Anything after the closing delimiter is the markdown
    body. Raises :class:`TemplateMarkdownError` when the block is missing,
    unbalanced, or the YAML is not a mapping.
    """
    if not isinstance(source, str):
        raise TemplateMarkdownError("template source must be a string")

    lines = source.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise TemplateMarkdownError("missing frontmatter: source must start with a '---' line")

    try:
        close_index = next(index for index in range(1, len(lines)) if lines[index].strip() == FRONTMATTER_DELIMITER)
    except StopIteration:
        raise TemplateMarkdownError("unbalanced frontmatter: no closing '---' line") from None

    yaml_block = "\n".join(lines[1:close_index])
    try:
        metadata = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise TemplateMarkdownError(f"invalid YAML frontmatter: {exc}") from exc

    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise TemplateMarkdownError("frontmatter must be a YAML mapping")

    body = "\n".join(lines[close_index + 1 :]).lstrip("\n")
    return metadata, body


def sanitize_markdown_html(html):
    """Strip dangerous HTML from rendered markdown, keeping legit output."""
    return nh3.clean(html, tags=_SANITIZE_TAGS, attributes=_SANITIZE_ATTRIBUTES, link_rel=None)


def render_markdown_html(markdown_source):
    """Convert context-rendered markdown to sanitized HTML."""
    return sanitize_markdown_html(
        markdown_to_html(markdown_source or "", extensions=MARKDOWN_EXTENSIONS),
    )


def markdown_to_plain_text(markdown_source):
    """Derive readable plain text from context-rendered markdown.

    Links become ``label (url)`` (or just the url when the label equals it),
    emphasis markers, backticks, heading markers, and quote markers are
    stripped, and blank-line paragraph structure is preserved so the text
    part stays readable in a mail client.
    """
    if not markdown_source:
        return ""

    text = _LINK_RE.sub(_link_replacement, markdown_source)
    text = _BOLD_RE.sub(r"\g<text>", text)
    text = _ITALIC_RE.sub(r"\g<text>", text)
    text = _CODE_RE.sub(r"\g<text>", text)

    plain_lines = []
    for line in text.splitlines():
        if _RULE_RE.match(line):
            continue
        line = _HEADING_RE.sub("", line)
        line = _QUOTE_RE.sub("", line)
        plain_lines.append(line.rstrip())

    text = "\n".join(plain_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _link_replacement(match):
    label = match.group("label").strip()
    url = match.group("url")
    if not label or label == url:
        return url
    return f"{label} ({url})"
