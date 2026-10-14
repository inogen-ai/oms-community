"""What a review card shows of a file's or a part's text.

A reviewer may take an AI-instruction flag as it is only when the card shows its words. The file
preview route and the safety check both read these rules from here, so the card and the rule
cannot disagree.

A file's main preview holds at most its first 64 KB and 1,000 lines. Beyond it, the card shows
excerpts: windows of 20 lines either side of each flagged line, merged when they touch, starting
after the lines the preview holds, at most 20 windows and 820 lines in all, each line cut at 2,000
characters, with a CRLF file's carriage return dropped as in the main preview. Lines are numbered
as the checks number them: only a line feed ends a line. Any other part (a section, rule, example
or field) is shown as one bounded view of 64 KB and 1,000 lines.
"""
from collections.abc import Iterable
from dataclasses import dataclass
import json
import re

PREVIEW_BYTES = 65536
PREVIEW_LINES = 1000
EXCERPT_CONTEXT = 20
EXCERPT_LIMIT = 20
EXCERPT_LINE_CHARS = 2000
_TOTAL = EXCERPT_LIMIT * (2 * EXCERPT_CONTEXT + 1)
# The bounded view of a part counts every kind of line break, which never counts fewer lines.
_BREAKS = re.compile(r"\r\n|\n|\r")


@dataclass(frozen=True)
class Preview:
    text: str
    truncated: bool
    # Lines the preview holds whole (all of them when it is not truncated); a line it shows only in
    # part is not held.
    held: int


def file_preview(text: str, body: bytes | None = None) -> Preview:
    """The main preview of a text file, whose bytes are `body` (by default, `text` in UTF-8)."""
    head = (text[:PREVIEW_BYTES].encode("utf-8", errors="surrogatepass") if body is None else body)[:PREVIEW_BYTES]
    bounded = "".join(head.decode("utf-8", errors="ignore").splitlines(keepends=True)[:PREVIEW_LINES])
    truncated = bounded != text
    return Preview(bounded, truncated, bounded.count("\n") if truncated else text.count("\n") + 1)


def excerpt_windows(line_count: int, held: int, flagged: Iterable[int]) -> list[tuple[int, int]]:
    """The first and last line of each excerpt window, in order."""
    windows: list[list[int]] = []
    total = 0
    for line in sorted({n for n in flagged if held < n <= line_count}):
        start = max(held + 1, line - EXCERPT_CONTEXT)
        end = min(line_count, line + EXCERPT_CONTEXT)
        if windows and start <= windows[-1][1] + 1:
            last = windows[-1]
            merged_end = max(last[1], end)
            room = _TOTAL - (total - (last[1] - last[0] + 1))
            if merged_end - last[0] + 1 > room:
                # The merged window is cut at the cap only while it still holds this line.
                if last[0] + room - 1 >= line:
                    total += last[0] + room - 1 - last[1]
                    last[1] = last[0] + room - 1
                break
            total += merged_end - last[1]
            last[1] = merged_end
        else:
            if len(windows) >= EXCERPT_LIMIT or total + end - start + 1 > _TOTAL:
                break
            windows.append([start, end])
            total += end - start + 1
    return [(start, end) for start, end in windows]


def _line(row: str) -> str:
    # A CRLF file's carriage return is not part of the line, as in the main preview.
    return row[:-1] if row.endswith("\r") else row


def excerpts(text: str, held: int, flagged: Iterable[int]) -> list[tuple[int, list[str], list[int]]]:
    """Each excerpt as its first line number, its lines cut at 2,000 characters, and the lines cut."""
    lines = text.split("\n")
    result = []
    for start, end in excerpt_windows(len(lines), held, flagged):
        rows = [_line(row) for row in lines[start - 1:end]]
        result.append((start, [row[:EXCERPT_LINE_CHARS] for row in rows],
                       [start + at for at, row in enumerate(rows) if len(row) > EXCERPT_LINE_CHARS]))
    return result


def file_shows(text: str, flagged: Iterable[int], spans: Iterable[tuple[int, int]]) -> bool:
    """Whether the card shows the words of every match in `spans` (offsets into `text`).

    `flagged` is every line the file's findings name, as the card builds its excerpts from them.
    Each line a match touches must be held whole by the main preview, or sit in an excerpt window
    with the match's part of it inside the line's first 2,000 characters.
    """
    ordered = sorted(spans)
    preview = file_preview(text)
    # The lines the preview holds end at its last line feed: a match that ends there is shown.
    if not ordered or not preview.truncated or max(end for _, end in ordered) <= preview.text.rfind("\n") + 1:
        return True
    windows = excerpt_windows(text.count("\n") + 1, preview.held, flagged)
    line, counted, begins = 1, 0, 0
    ends: dict[int, int] = {}   # where each line a match touched stops, found once per line
    for start, end in ordered:
        breaks = text.count("\n", counted, start)
        if breaks:
            line, begins = line + breaks, text.rfind("\n", counted, start) + 1
        counted = start
        number, at = line, begins
        while True:
            stop = ends.get(at)
            if stop is None:
                found = text.find("\n", at)
                stop = ends[at] = len(text) if found < 0 else found
            if number > preview.held:
                if not any(first <= number <= last for first, last in windows):
                    return False
                # The excerpt drops a CRLF file's carriage return, then cuts the line. The whole
                # line must fit: an instruction can go on after the words the screen matched.
                content = stop - 1 if stop > at and text[stop - 1] == "\r" else stop
                if content - at > EXCERPT_LINE_CHARS:
                    return False
            if end <= stop + 1:
                break
            number, at = number + 1, stop + 1
    return True


def part_view_fits(value: object) -> bool:
    """Whether the bounded view of a part's value, as the card writes it out, holds all of it."""
    shown = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    # A lone surrogate is counted as the escape that stands for it, never as fewer bytes.
    return (len(shown) <= PREVIEW_BYTES and len(shown.encode("utf-8", errors="backslashreplace")) <= PREVIEW_BYTES
            and len(_BREAKS.findall(shown)) < PREVIEW_LINES)
