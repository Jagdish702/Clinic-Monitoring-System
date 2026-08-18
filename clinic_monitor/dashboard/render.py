"""
Minimal Markdown to HTML for the report viewer.

The reports use a deliberately small subset - headings, tables, bold, lists and
paragraphs - so a focused converter is safer than pulling in a dependency, and
it lets everything stay inline (the dashboard makes no external requests).

All text is HTML-escaped before any formatting is applied, so report content
can never inject markup into the page.
"""

from __future__ import annotations

import html
import re
from typing import List

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_TABLE_DIVIDER = re.compile(r"^\|[\s:|-]+\|$")


def _inline(text: str) -> str:
    """Escape first, then apply inline formatting to the escaped text."""
    out = html.escape(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    return out


def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(text: str) -> str:
    """Convert a report to an HTML fragment."""
    lines = text.splitlines()
    out: List[str] = []
    index = 0
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while index < len(lines):
        line = lines[index].rstrip()

        if not line.strip():
            close_list()
            index += 1
            continue

        # Table: a header row, a divider, then body rows.
        if (
            line.startswith("|")
            and index + 1 < len(lines)
            and _TABLE_DIVIDER.match(lines[index + 1].strip())
        ):
            close_list()
            headers = _cells(line)
            out.append('<div class="table-wrap"><table><thead><tr>')
            out.extend(f"<th>{_inline(h)}</th>" for h in headers)
            out.append("</tr></thead><tbody>")
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                out.append("<tr>")
                out.extend(f"<td>{_inline(c)}</td>" for c in _cells(lines[index]))
                out.append("</tr>")
                index += 1
            out.append("</tbody></table></div>")
            continue

        if line.startswith("#"):
            close_list()
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{min(level, 4)}>{_inline(line.lstrip('#').strip())}"
                       f"</h{min(level, 4)}>")
            index += 1
            continue

        if line.lstrip().startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line.lstrip()[2:])}</li>")
            index += 1
            continue

        close_list()
        # Markdown treats two trailing spaces as a line break; the report
        # header uses that for its metadata block.
        out.append(f"<p>{_inline(line.strip())}</p>")
        index += 1

    close_list()
    return "\n".join(out)
