"""
The daily .xlsx the dashboard offers for download.

A spreadsheet is not a CSV with a different extension. The point of handing
someone Excel rather than comma-separated text is that the file arrives ready
to work with: times that sort as times, counts that add up, a header that stays
put when you scroll, and the rows that need attention visible without reading
every one. That is what this builds.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Sequence

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
except ImportError:                                   # pragma: no cover
    Workbook = None


HEADERS = (
    ("State", 16),
    ("Cluster", 16),
    ("Clinic", 26),
    ("Date", 12),
    ("Opening time", 14),
    ("Closing time", 14),
    ("Status", 12),
    ("Offline minutes", 16),
    ("Checks", 9),
)

# Excel's own palette conventions: red for a problem, amber for a caveat,
# green for fine. Kept pale so the text stays readable when printed.
_FILL = {
    "opened": PatternFill("solid", fgColor="E8F5E9") if Workbook else None,
    "closed": PatternFill("solid", fgColor="FFF4E5") if Workbook else None,
    "offline": PatternFill("solid", fgColor="FDE7E9") if Workbook else None,
}
_TEXT = {
    "opened": "1B5E20",
    "closed": "8A5300",
    "offline": "B3261E",
}


def _as_time(value: str) -> Optional[time]:
    """"08:42" -> a real time, so Excel sorts and formats it as one."""
    if not value:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":")[:2])
        return time(hour, minute)
    except (ValueError, TypeError):
        return None


def _as_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def build_workbook(day: str, rows: Sequence[Dict[str, Any]]) -> "Workbook":
    """A one-sheet workbook for a single day, formatted and ready to read."""
    if Workbook is None:
        raise RuntimeError(
            "openpyxl is not installed on this host, so the Excel download is "
            "unavailable - install it with 'pip install openpyxl'"
        )

    book = Workbook()
    sheet = book.active
    sheet.title = day                      # e.g. "2026-08-25"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="37474F")
    thin = Side(style="thin", color="D0D7DE")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    centre = Alignment(horizontal="center", vertical="center")

    for index, (title, width) in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=1, column=index, value=title)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.border = border
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 26

    for offset, row in enumerate(rows):
        line = offset + 2
        status = row.get("status", "")
        values = (
            row.get("state_name", ""),
            row.get("cluster", ""),
            row.get("clinic", ""),
            _as_date(row.get("date", "")) or row.get("date", ""),
            _as_time(row.get("opening_time", "")),
            _as_time(row.get("closing_time", "")),
            status,
            int(row.get("offline_minutes") or 0),
            int(row.get("checks") or 0),
        )
        for index, value in enumerate(values, start=1):
            cell = sheet.cell(row=line, column=index, value=value)
            cell.border = border
            if index == 4:
                cell.number_format = "yyyy-mm-dd"
                cell.alignment = centre
            elif index in (5, 6):
                # A blank means nobody was ever seen, which is information; an
                # empty cell says it more clearly than "00:00" would.
                cell.number_format = "hh:mm"
                cell.alignment = centre
            elif index in (8, 9):
                cell.number_format = "0"
                cell.alignment = centre
            elif index == 7:
                cell.alignment = centre
                if _FILL.get(status):
                    cell.fill = _FILL[status]
                    cell.font = Font(bold=True, color=_TEXT.get(status, "000000"))

    last = len(rows) + 1
    if rows:
        # A real Excel table: banded rows, and every column filterable and
        # sortable from the header without anyone setting that up by hand.
        span = f"A1:{get_column_letter(len(HEADERS))}{last}"
        table = Table(displayName="DailyReport", ref=span)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleLight1", showRowStripes=True, showColumnStripes=False
        )
        sheet.add_table(table)

    sheet.freeze_panes = "A2"              # header stays put while scrolling

    notes = book.create_sheet("What the columns mean")
    for line in _legend(day, rows):
        notes.append(line)
    notes.column_dimensions["A"].width = 20
    notes.column_dimensions["B"].width = 92
    for cell in notes["A"]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")
    for cell in notes["B"]:
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    return book


OFFLINE_HEADERS = (
    ("State", 16), ("Cluster", 16), ("Clinic", 26),
    ("Offline from", 12), ("Back at", 14), ("Status", 12),
    ("Duration (min)", 14), ("Checks", 9), ("Reason", 30),
)

CAMERA_HEADERS = (
    ("State", 16), ("Cluster", 16), ("Clinic", 26), ("Camera", 16),
    ("Checks", 9), ("Bad checks", 12), ("Dead all day", 14),
)

_HEADER_FONT = Font(bold=True, color="FFFFFF") if Workbook else None
_HEADER_FILL = PatternFill("solid", fgColor="37474F") if Workbook else None
if Workbook:
    _thin = Side(style="thin", color="D0D7DE")
    _BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
else:
    _BORDER = None
_BAD_FILL = PatternFill("solid", fgColor="FDE7E9") if Workbook else None
_BAD_FONT_COLOR = "B3261E"


def _as_time_from_iso(value: Optional[str]) -> Optional[time]:
    """A stored ISO timestamp's time-of-day, so Excel sorts and formats it
    as a real time instead of leaving it as text."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).time()
    except ValueError:
        return None


def _write_header(sheet, headers: Sequence[tuple]) -> None:
    for index, (title, width) in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index, value=title)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.border = _BORDER
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 26


def _finish_sheet(sheet, name: str, n_rows: int, n_cols: int) -> None:
    if n_rows:
        span = f"A1:{get_column_letter(n_cols)}{n_rows + 1}"
        table = Table(displayName=name, ref=span)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleLight1", showRowStripes=True, showColumnStripes=False
        )
        sheet.add_table(table)
    sheet.freeze_panes = "A2"


def build_offline_workbook(
    day: str,
    outages: Sequence[Dict[str, Any]],
    cameras: Sequence[Dict[str, Any]],
) -> "Workbook":
    """
    A two-sheet workbook for one day's Offline Clinics view: every clinic
    that could not be reached at all, and every camera that reported no
    signal on at least one check that day - the same two categories the
    dashboard's own overlay shows, kept on separate sheets since they
    track different things (a whole device down vs. one dead channel on
    an otherwise-reachable device).
    """
    if Workbook is None:
        raise RuntimeError(
            "openpyxl is not installed on this host, so the Excel download is "
            "unavailable - install it with 'pip install openpyxl'"
        )

    book = Workbook()
    sheet = book.active
    sheet.title = "Could not be reached"
    _write_header(sheet, OFFLINE_HEADERS)
    for offset, row in enumerate(outages):
        line = offset + 2
        ongoing = bool(row.get("ongoing"))
        values = (
            row.get("state", ""), row.get("cluster", ""), row.get("clinic_name", ""),
            _as_time_from_iso(row.get("from")),
            "Still offline" if ongoing else _as_time_from_iso(row.get("to")),
            "Ongoing" if ongoing else "Resolved",
            int(round(row.get("minutes") or 0)),
            int(row.get("checks") or 0),
            row.get("reason") or "",
        )
        for index, value in enumerate(values, start=1):
            cell = sheet.cell(row=line, column=index, value=value)
            cell.border = _BORDER
            if index in (4, 5):
                cell.alignment = Alignment(horizontal="center")
                if isinstance(value, time):
                    cell.number_format = "hh:mm"
            elif index in (7, 8):
                cell.number_format = "0"
                cell.alignment = Alignment(horizontal="center")
            elif index == 6:
                cell.alignment = Alignment(horizontal="center")
                if ongoing:
                    cell.fill = _BAD_FILL
                    cell.font = Font(bold=True, color=_BAD_FONT_COLOR)
    _finish_sheet(sheet, "Outages", len(outages), len(OFFLINE_HEADERS))

    cam_sheet = book.create_sheet("Camera issues")
    _write_header(cam_sheet, CAMERA_HEADERS)
    for offset, row in enumerate(cameras):
        line = offset + 2
        all_day = bool(row.get("all_day"))
        values = (
            row.get("state", ""), row.get("cluster", ""), row.get("clinic_name", ""),
            row.get("camera_name", ""), int(row.get("checks") or 0),
            int(row.get("bad") or 0), "Yes" if all_day else "No",
        )
        for index, value in enumerate(values, start=1):
            cell = cam_sheet.cell(row=line, column=index, value=value)
            cell.border = _BORDER
            if index in (5, 6):
                cell.number_format = "0"
                cell.alignment = Alignment(horizontal="center")
            elif index == 7:
                cell.alignment = Alignment(horizontal="center")
                if all_day:
                    cell.fill = _BAD_FILL
                    cell.font = Font(bold=True, color=_BAD_FONT_COLOR)
    _finish_sheet(cam_sheet, "CameraIssues", len(cameras), len(CAMERA_HEADERS))

    notes = book.create_sheet("What the columns mean")
    for line in _offline_legend(day, outages, cameras):
        notes.append(line)
    notes.column_dimensions["A"].width = 20
    notes.column_dimensions["B"].width = 92
    for cell in notes["A"]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")
    for cell in notes["B"]:
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    return book


def _offline_legend(
    day: str, outages: Sequence[Dict[str, Any]], cameras: Sequence[Dict[str, Any]]
) -> List[Sequence[str]]:
    ongoing = sum(1 for o in outages if o.get("ongoing"))
    all_day = sum(1 for c in cameras if c.get("all_day"))
    return [
        ("Offline clinics report", day),
        ("Clinics that could not be reached", f"{len(outages)} outage(s), "
                                              f"{ongoing} still ongoing"),
        ("Camera issues", f"{len(cameras)} camera(s) with at least one bad "
                          f"check, {all_day} dead all day"),
        ("", ""),
        ("Could not be reached", "The whole device was unreachable for a "
                                 "stretch of time - the patrol could not "
                                 "open it at all, not just one camera on it."),
        ("Offline from / Back at", "Times come from patrol checks, so each "
                                   "end is accurate to about one lap, not "
                                   "the minute. A clinic was last seen "
                                   "working before the 'offline from' time "
                                   "shown."),
        ("Still offline", "The device was still unreachable as of the most "
                          "recent check in this report."),
        ("", ""),
        ("Camera issues", "The device itself was reachable, but this one "
                          "camera channel reported no signal on at least "
                          "one check that day."),
        ("Dead all day", "This channel reported no signal on every single "
                         "check that day, not just some of them."),
    ]


def _legend(day: str, rows: Sequence[Dict[str, Any]]) -> List[Sequence[str]]:
    """
    The second sheet, so a figure is never read as more than it is.

    Whoever opens this weeks from now will not have been in the conversation
    where the caveats were explained, and a spreadsheet invites being trusted
    to the minute.
    """
    counts = {state: sum(1 for r in rows if r.get("status") == state)
              for state in ("opened", "closed", "offline")}
    return [
        ("Daily report", day),
        ("Clinics", f"{len(rows)} - {counts['opened']} opened, "
                    f"{counts['closed']} closed, {counts['offline']} offline"),
        ("", ""),
        ("Status", ""),
        ("opened", "Somebody was seen inside the clinic, so it was working."),
        ("closed", "The cameras worked and nobody was ever seen."),
        ("offline", "The device could not be reached, so nothing was watched. "
                    "This is NOT the same as closed - a clinic whose NVR drops "
                    "off the network looks identical to a shut one here, and "
                    "the fault is the connection, not the staff."),
        ("", ""),
        ("Opening time", "The first time a person was seen, not the moment the "
                         "door opened. The clinic may well have been working "
                         "earlier: somebody sitting still in a dim room is not "
                         "always picked up. Treat it as 'open by at least this "
                         "time'."),
        ("Closing time", "The last time a person was seen, read the same way."),
        ("Blank times", "Nobody was seen all day, or the device was offline."),
        ("Offline minutes", "How long the app reported the device unreachable. "
                            "Time inside this window was not watched at all, so "
                            "the opening and closing times cannot account for "
                            "it."),
        ("Checks", "How many patrol visits the row rests on. A day built from "
                   "3 checks is far weaker evidence than one built from 38 - "
                   "compare this before comparing times."),
        ("", ""),
        ("Cameras", "Times come from the indoor camera only. An outdoor view "
                    "cannot tell whether a clinic is working: an empty street "
                    "in the evening looks shut either way."),
    ]
