"""Turn one eCFR part snapshot into citable provisions. Pure: no network, no clock.

eCFR paragraphs are flat <P> elements; the hierarchy exists only as leading markers
like "(f)(5)(i)", six levels deep, with letters, numbers and roman numerals reused
across levels. This module rebuilds the tree from marker order.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

LEVEL_KINDS = ("lower", "arabic", "roman", "upper", "arabic", "roman", "upper")
PLAIN_LEVELS = range(0, 4)
ITALIC_LEVELS = range(4, 7)  # italic (1), (i), (A), by Federal Register convention

ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
EM_DASH, EN_DASH = "\u2014", "\u2013"  # how eCFR joins headings to subparagraphs
OPEN, CLOSE, ITALIC = "\x01", "\x02", "\x03"  # <I>, </I>, italic-marker flag
MARK = rf"{ITALIC}?\([0-9a-zA-Z]+\)"
LEAD = re.compile(
    rf"^\s*(?P<it>{ITALIC})?\((?P<m>[0-9a-zA-Z]+)\)"
    rf"(?:\s*[-–]\s*{ITALIC}?\((?P<end>[0-9a-zA-Z]+)\))?\s*"  # "(21)-(29) [Reserved]"
    rf"(?:{OPEN}(?P<h>[^{CLOSE}]*){CLOSE})?"
)
JOIN = re.compile(
    rf"^\s*(?P<sep>{EM_DASH}|{EN_DASH}|-|\.)?\s*"
    rf"(?={MARK}[\s({OPEN}{ITALIC}{EN_DASH}-])"  # "-": a range like "(i)-(ii)"
)
BLOCKS = ("P", "FP", "NOTE", "TABLE", "GPOTABLE", "EXTRACT", "FTNT", "DIV")
TABLES = ("TABLE", "GPOTABLE")
TITLES = ("CAPTION", "TTITLE", "HD", "HD1", "HD2", "HD3", "HED", "P")  # P: gpotbl_title
NAVIGATION = re.compile(r"\b(?:Section|Paragraph) Contents\b")

STUDENTS = ("F-1", "F-2", "M-1", "M-2")
CLASS_BY_LETTER = {  # 214.2 is lettered by visa class; checked against headings
    "a": "A",
    "b": "B",
    "c": "C",
    "d": "D",
    "e": "E",
    "g": "G",
    "h": "H",
    "i": "I",
    "k": "K",
    "l": "L",
    "n": "N",
    "o": "O",
    "p": "P",
    "q": "Q",
    "r": "R",
    "s": "NATO",
    "t": "S",
    "v": "V",
    "w": "CW",
}
VISA_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (  # docs/ingestion.md
    ("8 CFR 214.2(f)", ("F-1", "F-2")),
    ("8 CFR 214.2(j)", ("J-1", "J-2")),
    ("8 CFR 214.2(m)", ("M-1", "M-2")),
    *((f"8 CFR 214.2({k})", (v,)) for k, v in CLASS_BY_LETTER.items()),
    ("8 CFR 214.3", STUDENTS),  # school certification for F and M
    ("8 CFR 214.4", STUDENTS),
    ("8 CFR 214.5", ("LIBYA",)),
    ("8 CFR 214.6", ("TN",)),
    ("8 CFR 214.7", ("FAS",)),
    ("8 CFR 214.11", ("T",)),
    ("8 CFR 214.12", STUDENTS),  # preliminary SEVIS enrollment of schools
    ("8 CFR 214.13", ("F-1", "J-1", "M-1")),  # SEVIS fee, paid by principals
    ("8 CFR 214.14", ("U",)),
    ("8 CFR 214.15", ("V",)),
    *((f"8 CFR 214.{n}", ("T",)) for n in range(200, 217)),
    ("22 CFR 62", ("J-1", "J-2")),
)


@dataclass
class Provision:
    provision_id: str
    parent_id: str | None
    heading: str
    heading_path: str
    text: str = ""
    visa_types: tuple[str, ...] = ()
    visa_types_rule: str = ""


@dataclass(frozen=True)
class Marker:
    label: str  # as cited: "i", "A", "21)-(29"
    italic: bool
    heading: str
    last: str  # the marker that sets the counter: the end of a range


@dataclass
class ParseReport:
    provisions: list[Provision]
    guesses: list[str]  # markers with no single valid reading; should stay empty
    gaps: list[str]  # markers that skipped ahead, e.g. after a removed paragraph


@dataclass
class _State:
    last: list[int | None] = field(default_factory=lambda: [None] * len(LEVEL_KINDS))
    depth: int = 0  # number of open levels

    def copy(self) -> "_State":
        return _State(list(self.last), self.depth)


def _roman(s: str) -> int | None:
    if not s or any(c not in ROMAN for c in s):
        return None
    total = 0
    for a, b in zip(s, s[1:] + " ", strict=True):
        v = ROMAN[a]
        total += -v if ROMAN.get(b, 0) > v else v
    return total


def _letters(s: str) -> int | None:
    if s.isalpha() and len(set(s)) == 1:  # a..z, then aa, bb, ...
        return (len(s) - 1) * 26 + ord(s[0].lower()) - ord("a") + 1
    return None


def _value(marker: str, level: int) -> int | None:
    kind = LEVEL_KINDS[level]
    if kind == "arabic":
        return int(marker) if marker.isdigit() else None
    if kind == "lower":
        return _letters(marker) if marker.islower() else None
    if kind == "upper":
        return _letters(marker) if marker.isupper() else None
    return _roman(marker) if marker.islower() else None


def _levels(italic: bool) -> range:
    return ITALIC_LEVELS if italic else PLAIN_LEVELS


def _exact(state: _State, m: Marker, floor: int) -> list[int]:
    """Levels where the marker is the next item: a first child or the next sibling."""
    found = []
    for level in _levels(m.italic):
        value = _value(m.label.split(")")[0], level)
        if value is None or level <= floor:
            continue
        first_child = level == state.depth and value == 1
        next_sibling = level < state.depth and state.last[level] == value - 1
        if first_child or next_sibling:
            found.append(level)
    return found


def _skip(state: _State, m: Marker, floor: int) -> list[int]:
    """Fallback: an open list of the same kind that the marker skips ahead in."""
    found = []
    for level in _levels(m.italic):
        value = _value(m.label.split(")")[0], level)
        last = state.last[level]
        if value is not None and level > floor and level < state.depth and last:
            if last < value <= last + MAX_SKIP:
                found.append(level)
    return found[-1:]  # the deepest such list


def _exact_either(state: _State, m: Marker, floor: int) -> list[int]:
    """Italics are a strong hint, not a rule: old versions apply them unevenly."""
    return _exact(state, m, floor) or _exact(
        state, replace(m, italic=not m.italic), floor
    )


def _apply(state: _State, level: int, m: Marker) -> None:
    state.last[level] = _value(m.last, level)
    for deeper in range(level + 1, len(LEVEL_KINDS)):
        state.last[deeper] = None
    state.depth = level + 1


def _marked_text(p: ET.Element) -> str:
    """Paragraph text with italics wrapped in OPEN/CLOSE and italic markers flagged."""
    parts = [p.text or ""]
    for child in p:
        inner = "".join(child.itertext())
        parts.append(f"{OPEN}{inner}{CLOSE}" if child.tag == "I" else inner)
        parts.append(child.tail or "")
    text = "".join(parts)
    text = re.sub(
        rf"\s*([{EM_DASH}{EN_DASH}-])\s*\((\w+){CLOSE}\)", rf"{CLOSE}\1(\2)", text
    )  # a heading that swallowed the next marker: "<I>Guam, dash (1</I>)"
    text = re.sub(rf"\({OPEN}(\w+){CLOSE}\)", rf"{ITALIC}(\1)", text)  # (<I>1</I>)
    return re.sub(rf"{OPEN}\((\w+){CLOSE}\)", rf"{ITALIC}(\1)", text)  # <I>(1</I>)


DEFINITION = re.compile(
    rf"^\s*{OPEN}(?P<term>[^{CLOSE}]+){CLOSE}\s*[,:.]?\s*(?:{ITALIC}?\(1\)\s*)?"
    r"(?:[Mm]eans|has the (?:same )?meaning|refers to|includes|is defined"
    r"|for (?:the )?purposes? of)\b"
)
INLINE_LIST = re.compile(rf"(?<=[\s:]){ITALIC}\((?:1|i)\)\s")  # "means (1) One of"
LOOKALIKES = {"1": "i", "i": "1", "l": "1"}  # typos in the source, e.g. "(1)" for "(i)"
HEADING_ENDS = (EM_DASH, EN_DASH, "-", ".", "?", ":")
MAX_SKIP = 5  # removed paragraphs leave small gaps; larger jumps are misreadings


def _markers(text: str) -> tuple[list[Marker], str]:
    """Leading markers and the text after them, still marked up."""
    opened: list[Marker] = []
    rest = text
    while match := LEAD.match(rest):
        label, end = match.group("m"), match.group("end")
        heading = (match.group("h") or "").strip()
        opened.append(
            Marker(
                label=f"{label})-({end}" if end else label,
                italic=bool(match.group("it")),
                heading=heading.rstrip("".join(HEADING_ENDS)).strip(),
                last=end or label,
            )
        )
        rest = rest[match.end() :]
        join = JOIN.match(rest)
        if join is None:
            break
        after_heading = heading.endswith(HEADING_ENDS) or join.group("sep") is not None
        adjacent = not heading and join.end() == 0  # "(i)(A) Heading."
        if not (after_heading or adjacent):
            break
        rest = rest[join.end() :]
    return opened, rest


def _plain(text: str) -> str:
    for token in (ITALIC, OPEN, CLOSE):
        text = text.replace(token, "")
    return re.sub(r"\s+", " ", text).strip().lstrip(EM_DASH).strip()


def _split(text: str) -> list[tuple[list[Marker], str]]:
    """A paragraph as (markers opened, own text) pieces; usually one piece.

    A second piece appears when an italic list starts mid-sentence:
    "(L) Affiliate means (1) One of two subsidiaries".
    """
    opened, rest = _markers(text)
    pieces = []
    inline = INLINE_LIST.search(rest)
    if inline:
        more, tail = _markers(rest[inline.start() :])
        pieces.append((opened, _plain(rest[: inline.start()])))
        pieces.append((more, _plain(tail)))
    else:
        pieces.append((opened, _plain(rest)))
    if opened and opened[-1].heading and re.match(r"(means|has the)\b", pieces[0][1]):
        pieces[0] = (
            opened,
            f"{opened[-1].heading} {pieces[0][1]}",
        )  # "Affiliate means"
    return pieces


def _flat(el: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _table_text(table: ET.Element) -> str:
    rows = []
    for tr in table.iter("TR"):
        cells = [c for c in (_flat(cell) for cell in tr) if c]
        if cells:
            rows.append(": ".join(cells))
    return "; ".join(rows[1:] if len(rows) > 1 else rows)  # first row is the header


def _visa_types(provision_id: str) -> tuple[tuple[str, ...], str]:
    for prefix, visas in VISA_RULES:
        if provision_id == prefix or provision_id.startswith(
            (prefix + "(", prefix + ".", prefix + " ")  # " ": a section definition
        ):
            return visas, prefix
    return (), ""


def parse_part(xml: bytes, title: str) -> ParseReport:
    report = ParseReport([], [], [])
    for section in ET.fromstring(xml).iter("DIV8"):
        _parse_section(section, title, report)
    return report


def _parse_section(section: ET.Element, title: str, report: ParseReport) -> None:
    head = section.find("HEAD")
    heading = _flat(head).replace("§", "").strip() if head is not None else ""
    root = Provision(f"{title} CFR {section.get('N', '')}", None, heading, heading)
    root.visa_types, root.visa_types_rule = _visa_types(root.provision_id)
    report.provisions.append(root)
    seen = {root.provision_id}

    blocks = [el for el in section if el.tag in BLOCKS]
    marked = [_marked_text(el) if el.tag == "P" else "" for el in blocks]
    pieces = [
        _split(t) if el.tag == "P" else [] for el, t in zip(blocks, marked, strict=True)
    ]
    state = _State()
    open_nodes: list[tuple[float, Provision]] = [(-1.0, root)]  # (level, node)
    definitions: set[str] = set()

    def top() -> Provision:
        return open_nodes[-1][1]

    def append(text: str) -> None:
        if text:
            node = top()
            node.text = f"{node.text} {text}" if node.text else text

    def add(parent: Provision, pid: str, head: str, label: str) -> Provision:
        base, n = pid, 2
        while pid in seen:  # the same marker twice in the source
            pid, n = f"{base}#{n}", n + 1
        seen.add(pid)
        node = Provision(
            pid, parent.provision_id, head, f"{parent.heading_path} > {label}"
        )
        node.visa_types, node.visa_types_rule = _visa_types(pid)
        report.provisions.append(node)
        return node

    def upcoming(i: int, k: int, j: int) -> Marker | None:
        rest = [m for _, (ms, _) in [(0, p) for p in pieces[i][k:]] for m in ms][
            j + 1 :
        ]
        if rest:
            return rest[0]
        later = (ms[0] for p in pieces[i + 1 :] for ms, _ in p if ms)
        return next(later, None)

    for i, el in enumerate(blocks):
        if el.tag != "P":
            tables = [t for t in el.iter() if t.tag in TABLES]  # DIV can wrap one
            titles = " ".join(_flat(t) for t in el.iter() if t.tag in TITLES)
            if NAVIGATION.search(titles):  # contents tables, titled inside or beside
                continue
            for table in tables:
                append(_table_text(table))
            if not tables:
                append(_flat(el))
            continue
        definition = DEFINITION.match(marked[i]) if not pieces[i][0][0] else None
        if definition:
            earlier = [lv for lv, n in open_nodes if n.provision_id in definitions]
            level = earlier[-1] if earlier else state.depth - 0.5  # siblings share it
            while open_nodes[-1][0] >= level:
                open_nodes.pop()
            state.depth = int(level + 0.5)
            for deeper in range(state.depth, len(LEVEL_KINDS)):
                state.last[deeper] = None
            term = _plain(definition.group("term")).rstrip(",")
            node = add(top(), f'{top().provision_id} "{term}"', term, f'"{term}"')
            definitions.add(node.provision_id)
            open_nodes.append((level, node))
        floor = -1  # later markers in one paragraph are children of earlier ones
        for k, (opened, own) in enumerate(pieces[i]):
            if definition and k == 0 and not opened:  # its text includes the term
                append(own)
                continue
            for j, m in enumerate(opened):
                levels = _exact_either(state, m, floor)
                nxt = upcoming(i, k, j)
                if len(levels) > 1 and nxt is not None:
                    ahead = []
                    for level in levels:
                        trial = state.copy()
                        _apply(trial, level, m)
                        nxt_floor = level if nxt in opened else -1
                        if _exact_either(trial, nxt, nxt_floor):
                            ahead.append(level)
                    levels = ahead or levels  # a reading the next marker fits wins
                if len(levels) > 1 and nxt is None:  # a lone (i) is never a first child
                    levels = [lv for lv in levels if lv != state.depth] or levels
                where = f"{top().provision_id} then ({m.label}): {_flat(el)[:70]}"
                kinds = [
                    lv for lv in _levels(m.italic) if _value(m.last, lv) is not None
                ]
                if not levels and top().provision_id in definitions:
                    levels = [lv for lv in kinds if lv >= state.depth and lv > floor][
                        :1
                    ]
                if not levels and m.label in LOOKALIKES:
                    twin = replace(
                        m, label=LOOKALIKES[m.label], last=LOOKALIKES[m.label]
                    )
                    levels = _exact_either(state, twin, floor)
                    if levels:
                        report.gaps.append(f"lookalike {where}")
                        m = twin
                if not levels:
                    repeat = [
                        lv
                        for lv in kinds
                        if lv < state.depth and state.last[lv] == _value(m.last, lv)
                    ]
                    levels = repeat[-1:] or _skip(state, m, floor)
                    if levels:
                        report.gaps.append(where)
                if len(levels) != 1:
                    report.guesses.append(where)
                    levels = [max(levels)] if levels else kinds[:1] or [0]
                level = floor = levels[0]
                _apply(state, level, m)
                while open_nodes[-1][0] >= level:
                    open_nodes.pop()
                parent = top()
                node = add(
                    parent,
                    f"{parent.provision_id}({m.label})",
                    m.heading,
                    f"({m.label}) {m.heading}".strip(),
                )
                open_nodes.append((float(level), node))
            append(own)


def with_text(report: ParseReport) -> Iterator[Provision]:
    """Provisions that carry their own text; heading-only and reserved ones skipped."""
    return (p for p in report.provisions if p.text and p.text != "[Reserved]")
