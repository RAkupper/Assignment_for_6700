import gzip
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from myapp.ingest.ecfr import provision_url, records
from myapp.ingest.ecfr_parse import Provision, parse_part, with_text
from myapp.ingest.schema import RawDocument

FIXTURES = Path(__file__).with_name("fixtures")


def part(*paragraphs: str, section: str = "214.2", extra: str = "") -> bytes:
    body = "".join(f"<P>{p}</P>" for p in paragraphs)
    return (
        f'<DIV5 N="214"><DIV8 N="{section}"><HEAD>§ {section} Special rules.</HEAD>'
        f"{body}{extra}</DIV8></DIV5>"
    ).encode()


def parsed(*paragraphs: str, **kw: str) -> dict[str, Provision]:
    report = parse_part(part(*paragraphs, **kw), "8")
    assert report.guesses == []
    return {p.provision_id: p for p in with_text(report)}


def test_dash_opens_two_levels_in_one_paragraph() -> None:
    got = parsed(
        "(a) <I>General</I>\u2014(1) <I>Classes.</I> Text one.",
        "(2) Text two.",
    )
    assert got["8 CFR 214.2(a)(1)"].text == "Text one."
    assert got["8 CFR 214.2(a)(1)"].heading == "Classes"
    assert got["8 CFR 214.2(a)(2)"].parent_id == "8 CFR 214.2(a)"


def test_child_after_heading_period_and_question_mark() -> None:
    got = parsed(
        "(a) <I>Transfers.</I> (1) First.",
        "(2) Second.",
        "(b) <I>Who qualifies?</I> (1) Third.",
        "(2) Fourth.",
    )
    assert {"8 CFR 214.2(a)(1)", "8 CFR 214.2(b)(1)"} <= set(got)


def test_adjacent_markers() -> None:
    got = parsed("(a) <I>Petition</I>\u2014(1)(i) <I>Types.</I> Text.", "(ii) More.")
    assert "8 CFR 214.2(a)(1)(i)" in got


def test_italic_markers_are_levels_five_to_seven() -> None:
    got = parsed(
        "(a) A.",
        "(1) B.",
        "(i) C.",
        "(A) D:",
        "(<I>1</I>) E.",
        "(<I>i</I>) F.",
        "(<I>A</I>) G.",
        "(<I>B</I>) H.",
        "(<I>ii</I>) I.",
        "(<I>2</I>) J.",
        "(B) K.",
        "(2) L.",
    )
    deep = "8 CFR 214.2(a)(1)(i)(A)(1)(i)(A)"
    assert got[deep].text == "G."
    assert got["8 CFR 214.2(a)(2)"].text == "L."


def test_letter_i_versus_roman_i_by_what_follows() -> None:
    got = parsed(
        *[f"({c}) Para {c}." for c in "abcdefgh"],
        "(1) Under h.",
        "(i) Roman, because (ii) follows.",
        "(ii) Second roman.",
        "(2) Back to numbers.",
        "(i) Letter i, because (j) follows.",
        "(j) Letter j.",
    )
    assert got["8 CFR 214.2(h)(1)(i)"].text.startswith("Roman")
    assert got["8 CFR 214.2(i)"].text.startswith("Letter i")


def test_lone_last_i_is_a_letter_not_a_first_child() -> None:
    got = parsed(
        *[f"({c}) Para {c}." for c in "abcdefgh"], "(1) X.", "(2) Y.", "(i) Last."
    )
    assert got["8 CFR 214.2(i)"].text == "Last."


def test_definitions_and_their_lists() -> None:
    got = parsed(
        "(a) <I>Definitions.</I> As used here:",
        "<I>Employer</I> means a person that:",
        "(<I>1</I>) Has a job offer; and",
        "(<I>2</I>) Has a tax number.",
        "<I>Prominence</I> means achievement.",
        "<I>Affiliate</I> means (<I>1</I>) One of two subsidiaries, or",
        "(<I>2</I>) One of two entities.",
        "(b) Next.",
    )
    employer = '8 CFR 214.2(a) "Employer"'
    assert got[employer].parent_id == "8 CFR 214.2(a)"
    assert got[f"{employer}(2)"].text == "Has a tax number."
    assert got['8 CFR 214.2(a) "Prominence"'].parent_id == "8 CFR 214.2(a)"
    assert got['8 CFR 214.2(a) "Affiliate"(1)'].text.startswith(
        "One of two subsidiaries"
    )
    assert "8 CFR 214.2(b)" in got


def test_reserved_range_skips_ahead() -> None:
    got = parsed("(a) A.", "(1) X.", "(2)-(5) [Reserved]", "(6) After.")
    assert got["8 CFR 214.2(a)(6)"].text == "After."
    assert "8 CFR 214.2(a)(2)-(5)" not in got  # reserved, so no text to store


def test_duplicate_marker_is_kept_numbered_and_reported() -> None:
    report = parse_part(
        part("(a) A.", "(1) X.", "(2) Y.", "(2) Y again.", "(3) Z."), "8"
    )
    ids = [p.provision_id for p in with_text(report)]
    assert "8 CFR 214.2(a)(2)#2" in ids and "8 CFR 214.2(a)(3)" in ids
    assert report.guesses == [] and len(report.gaps) == 1


def test_lookalike_one_for_roman_i() -> None:
    report = parse_part(
        part("(a) A.", "(6) <I>Approval</I> - (1) <I>Ok.</I> X.", "(ii) Y."), "8"
    )
    ids = {p.provision_id for p in with_text(report)}
    assert "8 CFR 214.2(a)(6)(i)" in ids
    assert any(g.startswith("lookalike") for g in report.gaps)


def test_tables_notes_and_navigation() -> None:
    table = (
        "<TABLE><TR><TH>Section</TH><TH>Designation</TH></TR>"
        "<TR><TD>101(a)(15)(F)(i)</TD><TD>F-1.</TD></TR></TABLE>"
        "<TABLE><CAPTION>Table 1 to § 214.2\u2014Section Contents</CAPTION>"
        "<TR><TD>(a) Nav</TD></TR></TABLE>"
        "<NOTE><HED>Note 1:</HED><P>K-2 is the child of a K-1.</P></NOTE>"
    )
    got = parsed("(a) <I>Classes.</I> Designations are:", extra=table)
    text = got["8 CFR 214.2(a)"].text
    assert "101(a)(15)(F)(i): F-1." in text and "K-2 is the child" in text
    assert "Nav" not in text


def test_navigation_table_titled_beside_the_table_is_dropped() -> None:
    nav = (
        '<DIV><DIV class="table_head"><P class="gpotbl_title">Table 2 to Paragraph '
        "(f)\u2014Paragraph Contents</P></DIV><DIV><TABLE><TR><TD>(1) Admission"
        "</TD></TR><TR><TD>(2) Form I-20</TD></TR></TABLE></DIV></DIV>"
    )
    body = (
        '<DIV5 N="214"><DIV8 N="214.2"><HEAD>\u00a7 214.2 Rules.</HEAD>'
        "<P>(a) A.</P><P>(f) <I>Students.</I></P>" + nav + "<P>(1) Admission.</P>"
        "</DIV8></DIV5>"
    ).encode()
    got = {p.provision_id: p.text for p in with_text(parse_part(body, "8"))}
    assert "8 CFR 214.2(f)" not in got  # heading only once navigation is dropped
    assert got["8 CFR 214.2(f)(1)"] == "Admission."


def test_reserved_range_right_after_a_heading() -> None:
    got = parsed("(a) A.", "(1) <I>Old program</I>\u2014(i)-(ii) [Reserved]", "(2) B.")
    assert "8 CFR 214.2(a)(1)" not in got and "8 CFR 214.2(a)(2)" in got


def test_visa_types_follow_citation_rules() -> None:
    got = parsed(*[f"({c}) Para {c}." for c in "abcdefghij"], "(1) J one.")
    assert got["8 CFR 214.2(f)"].visa_types == ("F-1", "F-2")
    assert got["8 CFR 214.2(j)(1)"].visa_types_rule == "8 CFR 214.2(j)"
    assert got["8 CFR 214.2(h)"].visa_types == ("H",)
    assert got["8 CFR 214.2(i)"].visa_types == ("I",)


def test_section_rules_do_not_leak_into_longer_numbers() -> None:
    got = parsed("(a) <I>Definitions.</I> As used here:", "<I>Term</I> means X.")
    assert got["8 CFR 214.2(a)"].visa_types == ("A",)
    for section, visas in (
        ("214.1", ()),
        ("214.11", ("T",)),
        ("214.13", ("F-1", "J-1", "M-1")),
    ):
        got = parsed("(a) A.", "<I>Term</I> means X.", section=section)
        assert got[f"8 CFR {section}(a)"].visa_types == visas


def test_urls_point_at_the_paragraph_on_that_date() -> None:
    assert provision_url("8 CFR 214.2(f)(5)(i)", "2024-01-31") == (
        "https://www.ecfr.gov/on/2024-01-31/title-8/section-214.2#p-214.2(f)(5)(i)"
    )
    assert provision_url('8 CFR 214.2(h)(4)(ii) "Employer"', "2024-01-31").endswith(
        "section-214.2#p-214.2(h)(4)(ii)"
    )


@pytest.fixture(scope="module", params=["2020-01-01", "2026-09-30"])
def real(request: pytest.FixtureRequest) -> tuple[str, bytes]:
    day = request.param
    return day, gzip.open(FIXTURES / f"ecfr_8cfr214_{day}.xml.gz").read()


def test_real_8_cfr_214_parses_without_guesses(real: tuple[str, bytes]) -> None:
    _, body = real
    report = parse_part(body, "8")
    ids = [p.provision_id for p in report.provisions]
    assert report.guesses == []
    assert [k for k, n in Counter(ids).items() if n > 1] == []
    assert len(list(with_text(report))) > 2000
    assert len(report.gaps) <= 3  # real typos and removed paragraphs, reviewed


def test_real_citations_land_where_they_should(real: tuple[str, bytes]) -> None:
    _, body = real
    got = {p.provision_id: p for p in with_text(parse_part(body, "8"))}
    assert "F-1 student" in got["8 CFR 214.2(f)(5)(i)"].text
    assert got["8 CFR 214.2(f)(9)(i)"].visa_types == ("F-1", "F-2")
    assert "8 CFR 214.2(i)" in {p.parent_id for p in got.values()} | set(got)
    assert "101(a)(15)(F)(i): F-1" in got["8 CFR 214.1(a)(2)"].text


def test_records_are_valid_and_dated(real: tuple[str, bytes]) -> None:
    day, body = real
    raw = RawDocument(
        source="ecfr",
        url="https://www.ecfr.gov/api/versioner/v1/full/x",  # type: ignore[arg-type]
        retrieved_at=datetime(2026, 10, 27, tzinfo=UTC),
        content_type="application/xml",
        body=body,
        source_version=day,
    )
    out = list(records(raw, parse_part(body, "8")))
    assert all(
        r.metadata.valid_from and r.metadata.valid_from.isoformat() == day for r in out
    )
    assert all(r.metadata.authority == "regulation" for r in out)
