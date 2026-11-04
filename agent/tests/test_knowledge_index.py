"""The knowledge files (capability layer K3, spec 2026-10-07 §6.1; plan 2026-10-08-agent-k3 P6, P10, P12): the
frontmatter keys exactly as the spec names them, the four sections in order, a source for every line that states a
number -- and, from Tasks 2 and 3, INDEX.md's table and what retrieval takes. Files are written into a temporary
directory that stands in for agent/knowledge (agent/tests/knowledge_fixtures.py)."""

import logging
import re
from datetime import date

import pytest

from agent import consult
from agent import knowledge_index as ki
from agent.tests import knowledge_fixtures as kf


def test_a_valid_file_loads_as_an_entry(tmp_path):
    kf.write(tmp_path, "dentists", "google-search", tags=("stage:plan", "keywords"))
    [e] = ki.load(tmp_path)
    assert (e.id, e.vertical, e.topic, e.path) == (
        "dentists/google-search",
        "dentists",
        "google-search",
        tmp_path / "dentists" / "google-search.md",
    )
    assert e.channels == ("google_search",) and e.tags == ("stage:plan", "keywords")
    assert (e.summary, e.confidence, e.reviewed_by, e.reviewed_at) == (
        kf.SUMMARY,
        "medium",
        "founder",
        date(2026, 10, 9),
    )
    assert e.sources == ({"id": "src-one", "url": "https://example.com/one", "as_of": date(2026, 10, 5)},)
    assert ki.key_points(e) == "- Lead with the one service the owner picked."
    assert ki.do_not_say(e) == ["- Best dentist in town."]


def test_files_load_in_id_order_and_none_is_an_empty_list(tmp_path):
    kf.write(tmp_path, "home_services", "roofing")
    kf.write(tmp_path, "dentists", "offers", numbers=(), do_not_say=())
    entries = ki.load(tmp_path)
    assert [e.id for e in entries] == ["dentists/offers", "home_services/roofing"]
    assert ki.do_not_say(entries[0]) == []
    assert ki.load(tmp_path / "nowhere") == []


def test_a_vertical_is_a_consultation_trees_id():
    assert ki.TREES_DIR == consult.TREES_DIR


def test_a_file_written_for_review_loads_as_pending(tmp_path):
    kf.write(tmp_path, "dentists", "compliance", reviewed_by="pending")
    assert ki.load(tmp_path)[0].reviewed_by == ki.PENDING


SUMMARY_RULE = "with no digit and none of '|', '<' or '>'"  # the tail of the summary rule's message
BROKEN = [
    (lambda t: t[4:], "no frontmatter: the file starts with a line of three dashes"),
    (
        lambda t: t.replace("reviewed_at: 2026-10-09\n---\n", "reviewed_at: 2026-10-09\n"),
        "the frontmatter has no closing line of three dashes",
    ),
    (lambda t: t.replace("tags: [stage:plan]", "tags: [stage:plan"), "the frontmatter is not YAML"),
    # PyYAML reads 2026-02-30 as a date and raises a plain ValueError (not a YAMLError): still the file's fault
    (lambda t: t.replace("as_of: 2026-10-05", "as_of: 2026-02-30"), "the frontmatter is not YAML"),
    # and its constructors raise KeyError, IndexError and AttributeError on a malformed tag, RecursionError on nesting
    (lambda t: t.replace("confidence: medium", "confidence: !!bool maybe"), "the frontmatter is not YAML"),
    (lambda t: t.replace("confidence: medium", "confidence: !!float"), "the frontmatter is not YAML"),
    (lambda t: t.replace("reviewed_at: 2026-10-09", "reviewed_at: !!timestamp nope"), "the frontmatter is not YAML"),
    (lambda t: t.replace("tags: [stage:plan]", "tags: " + "[" * 600 + "]" * 600), "the frontmatter is not YAML"),
    (lambda t: t.replace("confidence: medium\n", ""), "(missing ['confidence'], unknown [])"),
    (
        lambda t: t.replace("confidence: medium\n", "confidence: medium\nsummary: x\n"),
        "(missing [], unknown ['summary'])",
    ),
    # YAML reads a key such as 1, on or ~ as an int, a bool or None, which sorted() cannot order among the names
    (lambda t: t.replace("confidence: medium\n", "confidence: medium\n1: a\nb: c\n"), "(missing [], unknown [1, 'b'])"),
    (lambda t: t.replace("vertical: dentists", "vertical: attorneys"), "vertical 'attorneys' is not its directory"),
    (lambda t: t.replace("[google_search]", "[tiktok]"), "channels must be a list of google_search"),
    (lambda t: t.replace("[stage:plan]", "[Stage Plan]"), "tags must be a list of lowercase words"),
    (lambda t: t.replace("    as_of: 2026-10-05\n", ""), "each source has an id, a url or a doc, and as_of"),
    (lambda t: t.replace("id: src-one", "id: Src One"), "a source id must be lowercase letters, digits and '-'"),
    (
        lambda t: t.replace('"https://example.com/one"', '"ftp://example.com/one"'),
        "a source's url starts with http:// or https://",
    ),
    (
        lambda t: t.replace('url: "https://example.com/one"', 'doc: "../secrets.md"'),
        "a source's doc is a path inside the repository",
    ),
    (lambda t: t.replace("as_of: 2026-10-05", "as_of: 2026-10"), "a source's as_of must be a date"),
    (lambda t: t.replace("confidence: medium", "confidence: sure"), "confidence must be one of low, medium, high"),
    (lambda t: t.replace("reviewed_by: founder", "reviewed_by: claude"), "reviewed_by must be one of founder, pending"),
    (lambda t: t.replace("reviewed_at: 2026-10-09", "reviewed_at: soon"), "reviewed_at must be a date"),
    (lambda t: t.replace("# Test knowledge", "Test knowledge"), "the body starts with a '# ' title"),
    (lambda t: t.replace(kf.SUMMARY, "Found on Google Search in 3 ways."), "under the title comes the summary"),
    # a summary is a cell of INDEX.md's table, whose markers are HTML comments: it holds neither bracket (K3 Part A's
    # final review: one holding the end marker ended the table early), nor a '|'
    (lambda t: t.replace(kf.SUMMARY, "Found on Google Search <!-- files:end --> in a town."), SUMMARY_RULE),
    (lambda t: t.replace(kf.SUMMARY, "Found on Google Search when a < b."), SUMMARY_RULE),
    (lambda t: t.replace(kf.SUMMARY, "Found on Google Search >."), SUMMARY_RULE),
    (lambda t: t.replace(kf.SUMMARY, "Found on Google Search | in a town."), SUMMARY_RULE),
    (lambda t: t.replace("## Numbers", "## Figures"), "(found Key points, Details, Figures, Do not say)"),
    (lambda t: t.replace("## Do not say", "## Key points"), "(found Key points, Details, Numbers, Key points)"),
    (lambda t: t.replace("- Lead with the one", "Lead with the one"), "Key points is a list of '- ' lines"),
    (
        lambda t: t.replace("- Best dentist in town.", "Best dentist in town."),
        "Do not say is a list of '- ' lines (or None.)",
    ),
    (
        lambda t: t.replace("on average [src-one]", "on average [src-two]"),
        "a line cites src-two, which its sources do not list",
    ),
    (
        lambda t: t.replace("and a town [src-one].", "and a town in 2026."),
        "a line states a number without a source: 'Searches name a service and a town in 2026.'",
    ),
    # spec §6.1: every Numbers line has its source id, digit or not (a line elsewhere needs one only with a digit)
    (
        lambda t: t.replace("- A lead costs $72.97 on average [src-one].", "- About half of callers book."),
        "a line states a number without a source: '- About half of callers book.'",
    ),
]


@pytest.mark.parametrize(("change", "says"), BROKEN)
def test_a_file_that_breaks_the_format_is_refused_with_its_id_and_the_rule(tmp_path, change, says):
    path = kf.write(tmp_path)
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ki.KnowledgeError, match=r"^dentists/google-search: .*" + re.escape(says)):
        ki.load(tmp_path)


def _spoil(path):
    """The file with a byte that is no UTF-8 (a Latin-1 'é'); BROKEN's rows edit text, so this edits bytes."""
    path.write_bytes(path.read_bytes().replace(b"Test knowledge", b"Test knowledge \xe9"))


def test_a_file_that_is_not_utf8_is_refused_with_its_id_and_the_rule(tmp_path):
    _spoil(kf.write(tmp_path))
    with pytest.raises(ki.KnowledgeError, match=r"^dentists/google-search: the file is not UTF-8"):
        ki.load(tmp_path)


def test_a_loaded_file_that_stops_being_utf8_is_refused_when_its_sections_are_read(tmp_path):
    path = kf.write(tmp_path)
    [e] = ki.load(tmp_path)
    _spoil(path)
    for read in (ki.key_points, ki.do_not_say):
        with pytest.raises(ki.KnowledgeError, match=r"^dentists/google-search: the file is not UTF-8"):
            read(e)


def test_a_directory_named_like_a_file_is_refused_with_its_id_and_the_rule(tmp_path):
    (tmp_path / "dentists" / "compliance.md").mkdir(parents=True)  # `*/*.md` matches it; reading it is an OSError
    with pytest.raises(ki.KnowledgeError, match=r"^dentists/compliance: the file cannot be read"):
        ki.load(tmp_path)


def test_a_loaded_file_that_goes_missing_is_refused_when_its_sections_are_read(tmp_path):
    path = kf.write(tmp_path)
    [e] = ki.load(tmp_path)
    path.unlink()
    for read in (ki.key_points, ki.do_not_say):
        with pytest.raises(ki.KnowledgeError, match=r"^dentists/google-search: the file cannot be read"):
            read(e)


def test_a_numbers_line_with_no_digit_loads_when_it_cites_a_source(tmp_path):
    kf.write(tmp_path, numbers=("About half of callers book [src-one].",))
    assert ki.load(tmp_path)


def test_a_source_no_line_cites_is_refused(tmp_path):
    kf.write(tmp_path, sources=(kf.SOURCE, kf.SOURCE_TWO))
    with pytest.raises(ki.KnowledgeError, match="dentists/google-search: the source src-two is never cited"):
        ki.load(tmp_path)


def test_key_points_hold_at_most_300_words(tmp_path):
    kf.write(tmp_path, key_points=(" ".join(["word"] * 300),))
    assert ki.load(tmp_path)
    kf.write(tmp_path, key_points=(" ".join(["word"] * 301),))
    with pytest.raises(ki.KnowledgeError, match="Key points has 301 words; at most 300"):
        ki.load(tmp_path)


def test_a_directory_that_is_no_tree_and_a_topic_out_of_shape_are_refused(tmp_path):
    kf.write(tmp_path / "a", "plumbers", "roofing")
    with pytest.raises(ki.KnowledgeError, match="plumbers/roofing: 'plumbers' is not a consultation tree's id"):
        ki.load(tmp_path / "a")
    kf.write(tmp_path / "b", "dentists", "Google_Search")
    with pytest.raises(ki.KnowledgeError, match="the topic must be lowercase words joined by '-'"):
        ki.load(tmp_path / "b")


def test_tokens_are_estimated_conservatively():
    assert ki.tokens("") == 0 and ki.tokens("abcdefg") == 2 and ki.tokens("x" * 5250) == ki.BUDGET_TOKENS


def test_the_index_block_has_one_line_per_file(tmp_path):
    kf.write(tmp_path, "home_services", "roofing", tags=("stage:plan", "trade:roofing"))
    kf.write(
        tmp_path,
        "dentists",
        "compliance",
        channels=(),
        sources=(kf.SOURCE, kf.SOURCE_TWO),
        details="Searches name a service [src-one] [src-two].",
        reviewed_by="pending",
    )
    assert ki.index_block(ki.load(tmp_path)) == "\n".join(
        [
            *ki.INDEX_HEAD,
            f"| dentists/compliance | any | stage:plan | {kf.SUMMARY} | 2 | medium | 2026-10-09 (pending) |",
            (
                f"| home_services/roofing | google_search | stage:plan, trade:roofing | {kf.SUMMARY} | 1 | medium | "
                "2026-10-09 (founder) |"
            ),
        ]
    )


def test_write_index_rewrites_the_files_table_and_nothing_else(tmp_path):
    kf.index(tmp_path)
    kf.write(tmp_path, "dentists", "offers")
    assert ki.write_index(tmp_path) is True
    whole = ki.index_text(tmp_path)
    assert whole.startswith("# Test knowledge index\n\n") and whole.endswith("\n\nThe end.\n")
    assert ki.index_files(tmp_path) == ki.index_block(ki.load(tmp_path))
    assert ki.write_index(tmp_path) is False


def test_a_summary_holding_the_end_marker_never_reaches_the_index(tmp_path):
    """K3 Part A's final review: `<!-- files:end -->` in a summary ended the table early. write_index then grew
    INDEX.md on every run, and served_index_text, which filters up to the first end marker, served the rows after it,
    a file waiting for its review among them (plan P10). The file is refused, so INDEX.md is not written from it."""
    kf.index(tmp_path)
    kf.write(tmp_path, "dentists", "compliance", reviewed_by="pending")
    kf.write(tmp_path, "dentists", "offers", summary=f"Found on Google Search {ki.FILES_END} in a town.")
    with pytest.raises(ki.KnowledgeError, match="^dentists/offers: under the title comes the summary"):
        ki.write_index(tmp_path)
    assert ki.index_text(tmp_path) == kf.INDEX_TEXT


def test_an_index_without_its_markers_is_refused(tmp_path):
    (tmp_path / "INDEX.md").write_text("# no markers\n", encoding="utf-8")
    with pytest.raises(ki.KnowledgeError, match="INDEX.md: the <!-- files:begin --> and <!-- files:end --> lines"):
        ki.write_index(tmp_path)
    # nor is it served: without the markers there is no table to take a pending file's row from
    with pytest.raises(ki.KnowledgeError, match="INDEX.md: the <!-- files:begin --> and <!-- files:end --> lines"):
        ki.served_index_text(tmp_path)


def test_the_served_index_is_the_file_as_it_ships_when_no_file_waits_for_its_review(tmp_path):
    kf.index(tmp_path)
    assert ki.served_index_text(tmp_path) == kf.INDEX_TEXT  # an empty table, byte for byte
    kf.write(tmp_path, "attorneys", "compliance", channels=())
    kf.write(tmp_path, "dentists", "offers", channels=())
    ki.write_index(tmp_path)
    assert "| dentists/offers |" in ki.index_text(tmp_path)
    assert ki.served_index_text(tmp_path) == ki.index_text(tmp_path)


def test_the_served_index_leaves_out_the_row_of_a_file_waiting_for_its_review_and_nothing_else(tmp_path):
    """Plan P10: no pending file reaches a resource. INDEX.md lists one (`(pending)` in its Reviewed cell: the founder
    reads the batch there); the served index drops that row, the rows of the reviewed files and the rest stay."""
    kf.index(tmp_path)
    kf.write(tmp_path, "attorneys", "compliance", channels=())
    kf.write(tmp_path, "dentists", "offers", channels=(), reviewed_by="pending")
    kf.write(tmp_path, "dentists", "google-search")
    ki.write_index(tmp_path)
    # a line outside the files table that happens to look like a pending row is not a file's row: it stays
    elsewhere = "| 2.8 | Dental | dentists/offers (pending) |\n"
    index = tmp_path / "INDEX.md"
    index.write_text(index.read_text(encoding="utf-8") + elsewhere, encoding="utf-8")
    shipped = ki.index_text(tmp_path)
    [row] = [line for line in shipped.splitlines(keepends=True) if line.startswith("| dentists/offers | any |")]
    assert "2026-10-09 (pending) |" in row  # the writer lists it, as the founder reads it
    served = ki.served_index_text(tmp_path)
    assert served == shipped.replace(row, "")
    assert served.count("2026-10-09 (founder) |") == 2 and served.endswith(elsewhere)


def test_the_cli_writes_the_index(tmp_path, monkeypatch, capsys):
    kf.index(tmp_path)
    kf.write(tmp_path)
    monkeypatch.setattr(ki, "KNOWLEDGE_DIR", tmp_path)
    assert ki.main(["index"]) == 0 and "INDEX.md written" in capsys.readouterr().out
    assert ki.main(["index"]) == 0 and "INDEX.md already lists every file" in capsys.readouterr().out


def test_an_index_that_is_missing_or_not_utf8_is_refused_with_its_name_and_the_rule(tmp_path):
    with pytest.raises(ki.KnowledgeError, match=r"^INDEX.md: the file cannot be read"):
        ki.index_text(tmp_path)
    (tmp_path / "INDEX.md").write_bytes(b"# Test knowledge index \xe9\n")
    with pytest.raises(ki.KnowledgeError, match=r"^INDEX.md: the file is not UTF-8"):
        ki.write_index(tmp_path)


def test_the_cli_names_a_file_that_breaks_the_format_and_leaves_the_index_alone(tmp_path, monkeypatch, capsys):
    kf.index(tmp_path)
    kf.write(tmp_path, numbers=("About half of callers book.",))  # a Numbers line with no source
    monkeypatch.setattr(ki, "KNOWLEDGE_DIR", tmp_path)
    assert ki.main(["index"]) == 1
    shown = capsys.readouterr()
    assert shown.out == "" and "dentists/google-search: a line states a number without a source" in shown.err
    assert ki.index_text(tmp_path) == kf.INDEX_TEXT


def test_retrieval_reads_the_callers_topics_first_then_the_newest_review(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, "dentists", "google-search", reviewed_at="2026-10-01")
    kf.write(root, "dentists", "compliance", channels=(), reviewed_at="2026-09-01")
    kf.write(root, "dentists", "offers", channels=(), reviewed_at="2026-10-05")
    kf.write(root, "attorneys", "compliance", channels=())
    wanted = ("compliance", "google-search", "offers")
    assert ki.retrieve("dentists", channels=("google_search",), topics=wanted).ids == [
        "dentists/compliance",
        "dentists/google-search",
        "dentists/offers",
    ]
    assert ki.retrieve("dentists").ids == ["dentists/offers", "dentists/google-search", "dentists/compliance"]
    assert ki.retrieve("dentists", topics=("offers",)).ids == ["dentists/offers"]


def test_a_file_for_another_channel_is_left_out_and_one_for_every_channel_kept(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, "dentists", "newsbreak", channels=("newsbreak",))
    kf.write(root, "dentists", "compliance", channels=())
    kf.write(root, "dentists", "google-search")
    assert ki.retrieve("dentists", channels=("google_search",)).ids == ["dentists/compliance", "dentists/google-search"]
    assert ki.retrieve("dentists", channels=("google_search", "newsbreak")).ids == [
        "dentists/compliance",
        "dentists/google-search",
        "dentists/newsbreak",
    ]


def test_a_tag_answers_a_topic(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, "home_services", "roofing", tags=("stage:plan", "trade:roofing"))
    kf.write(root, "home_services", "plumbing", tags=("stage:plan", "trade:plumbing"))
    assert ki.retrieve("home_services", topics=("trade:roofing",)).ids == ["home_services/roofing"]


def test_a_block_is_the_id_the_key_points_and_the_do_not_say_lines_without_source_ids(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(
        root,
        key_points=("Costs vary by town [src-one].", "Keep jobs out."),
        do_not_say=("Free exams in Florida [src-one].",),
    )
    [e] = ki.load(root)
    assert ki.block(e) == (
        "== dentists/google-search ==\n- Costs vary by town.\n- Keep jobs out.\nDo not say:\n- Free exams in Florida."
    )
    assert ki.retrieve("dentists") == ki.Retrieved(ki.block(e), ["dentists/google-search"])


def test_the_budget_takes_whole_blocks_and_skips_one_that_does_not_fit(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, "dentists", "compliance", channels=(), key_points=(" ".join(["word"] * 280),))
    kf.write(root, "dentists", "google-search")
    kf.write(root, "dentists", "offers", channels=())
    small = {e.id: ki.block(e) for e in ki.load(root) if e.topic != "compliance"}
    budget = ki.tokens(small["dentists/google-search"]) + ki.tokens(ki.SEPARATOR) + ki.tokens(small["dentists/offers"])
    got = ki.retrieve("dentists", topics=("compliance", "google-search", "offers"), budget_tokens=budget)
    assert got.ids == ["dentists/google-search", "dentists/offers"]
    assert "== dentists/compliance ==" not in got.text and ki.tokens(got.text) <= budget
    assert ki.retrieve("dentists", budget_tokens=0) == ki.Retrieved("", [])


def test_cited_reads_the_ids_in_their_order_and_skips_an_id_no_file_has(tmp_path, monkeypatch):
    kf.use(monkeypatch, kf.tree(tmp_path))
    got = ki.cited(["dentists/offers", "dentists/gone", "dentists/compliance", "dentists/offers"])
    assert got.ids == ["dentists/offers", "dentists/compliance"]
    assert got.text.startswith("== dentists/offers ==") and ki.cited([]) == ki.Retrieved("", [])


def test_a_pending_file_is_never_retrieved_nor_found(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, "dentists", "compliance", channels=(), reviewed_by="pending")
    kf.write(root, "dentists", "google-search")
    assert ki.retrieve("dentists").ids == ["dentists/google-search"]
    assert ki.cited(["dentists/compliance"]).ids == [] and ki.get("dentists/compliance") is None
    # the preview and the batch's own test read every loaded file, pending ones too
    assert ki.select(ki.load(root), "dentists").ids == ["dentists/compliance", "dentists/google-search"]


def test_a_malformed_file_never_stops_a_plan(tmp_path, monkeypatch, caplog):
    root = kf.use(monkeypatch, tmp_path)
    kf.index(root)
    kf.write(root, "dentists", "google-search")
    (root / "dentists" / "broken.md").write_text("no frontmatter", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="agent.knowledge_index"):
        assert ki.retrieve("dentists") == ki.Retrieved("", [])
        assert ki.cited(["dentists/google-search"]) == ki.Retrieved("", [])
        assert ki.get("dentists/google-search") is None
    assert "dentists/broken: no frontmatter" in caplog.text
    # the index resource still serves: it reads INDEX.md, not the files (Review Focus 2)
    assert ki.index_text() == ki.index_text(root) == ki.served_index_text() == kf.INDEX_TEXT


def test_get_and_file_text_serve_a_reviewed_file_verbatim(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    path = kf.write(root)
    assert ki.file_text(ki.get("dentists/google-search")) == path.read_text(encoding="utf-8")
    assert ki.get("dentists/nothing") is None


def test_the_preview_prints_what_a_draft_reads_pending_files_included(tmp_path, monkeypatch, capsys):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, reviewed_by="pending")
    assert ki.main(["preview", "dentists", "--channel", "google_search", "--topic", "google-search"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("== dentists/google-search ==") and "1 file(s): dentists/google-search" in err


def test_the_load_is_kept_until_it_is_forgotten(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root, "dentists", "google-search")
    assert ki.retrieve("dentists").ids == ["dentists/google-search"]
    kf.write(root, "dentists", "compliance", channels=())  # the files never change while the image runs: not re-read
    assert ki.retrieve("dentists").ids == ["dentists/google-search"]
    ki.forget()
    assert ki.retrieve("dentists").ids == ["dentists/compliance", "dentists/google-search"]


# file_text reads through _read like every other read of a knowledge file: a file that went since the load is a
# KnowledgeError naming it, never an OSError or a UnicodeDecodeError
def test_file_text_of_a_file_that_is_not_utf8_or_gone_is_a_knowledge_error_naming_it(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    path = kf.write(root)
    entry = ki.get("dentists/google-search")
    _spoil(path)
    with pytest.raises(ki.KnowledgeError, match=r"^dentists/google-search: the file is not UTF-8"):
        ki.file_text(entry)
    path.unlink()
    with pytest.raises(ki.KnowledgeError, match=r"^dentists/google-search: the file cannot be read"):
        ki.file_text(entry)


# block() reads its file again after the load was cached, so retrieval can meet a file that went bad since: that one is
# logged and left out, the others still read, and no plan stops on it
DAMAGE = [
    pytest.param(lambda path: path.unlink(), "the file cannot be read", id="gone"),
    pytest.param(_spoil, "the file is not UTF-8", id="not-utf8"),
    pytest.param(lambda path: path.write_text("no frontmatter", encoding="utf-8"), "no frontmatter", id="no-yaml"),
]


def _one_error(caplog, says):
    """The one ERROR line a file that went bad since the load makes: it names the file and the rule."""
    [line] = caplog.records
    assert line.levelno == logging.ERROR
    assert line.getMessage().startswith(f"knowledge: dentists/google-search: {says}")
    caplog.clear()


@pytest.mark.parametrize(("damage", "says"), DAMAGE)
def test_a_file_that_goes_bad_after_the_load_is_left_out_and_the_next_one_read(
    tmp_path, monkeypatch, caplog, damage, says
):
    root = kf.use(monkeypatch, kf.tree(tmp_path))
    wanted = ("compliance", "google-search", "offers")
    first = ki.retrieve("dentists", topics=wanted)  # the load is cached from here on
    assert first.ids == ["dentists/compliance", "dentists/google-search", "dentists/offers"]
    damage(root / "dentists" / "google-search.md")
    with caplog.at_level(logging.ERROR, logger="agent.knowledge_index"):
        got = ki.retrieve("dentists", topics=wanted)
    assert got.ids == ["dentists/compliance", "dentists/offers"] and "dentists/google-search" not in got.text
    _one_error(caplog, says)
    with caplog.at_level(logging.ERROR, logger="agent.knowledge_index"):
        got = ki.cited(["dentists/offers", "dentists/google-search", "dentists/compliance"])
    assert got.ids == ["dentists/offers", "dentists/compliance"] and "dentists/google-search" not in got.text
    _one_error(caplog, says)


def test_whatever_the_budget_the_text_is_whole_blocks_within_it(tmp_path, monkeypatch):
    root = kf.use(monkeypatch, tmp_path)
    for i, topic in enumerate(("alpha", "beta", "gamma", "delta")):  # four sizes; id order is alpha, beta, delta, gamma
        kf.write(root, "dentists", topic, channels=(), key_points=(" ".join(["word"] * (3 + 17 * i)),))
    whole = {e.id: ki.block(e) for e in ki.load(root)}
    top = sum(ki.tokens(b) for b in whole.values()) + (len(whole) - 1) * ki.tokens(ki.SEPARATOR)  # every block fits
    outcomes = set()
    for budget in range(top + 2):
        got = ki.retrieve("dentists", budget_tokens=budget)
        assert ki.tokens(got.text) <= budget
        assert got.text == ki.SEPARATOR.join(whole[i] for i in got.ids)  # whole blocks only, in the ids' order
        outcomes.add(tuple(got.ids))
    assert () in outcomes and ("dentists/alpha", "dentists/beta", "dentists/gamma") in outcomes  # delta skipped
    assert ki.retrieve("dentists", budget_tokens=top).ids == list(whole)
    assert ki.retrieve("dentists", budget_tokens=top - 1).ids != list(whole)  # the edge is exact


def test_the_preview_names_a_file_that_breaks_the_format_and_prints_no_block(tmp_path, monkeypatch, capsys):
    root = kf.use(monkeypatch, tmp_path)
    kf.write(root)
    (root / "dentists" / "broken.md").write_text("no frontmatter", encoding="utf-8")
    assert ki.main(["preview", "dentists"]) == 1
    shown = capsys.readouterr()
    assert shown.out == "" and "dentists/broken: no frontmatter" in shown.err


def test_the_preview_says_none_when_no_file_fits(tmp_path, monkeypatch, capsys):
    kf.use(monkeypatch, kf.tree(tmp_path))
    assert ki.main(["preview", "home_services"]) == 0
    assert "0 file(s): none; about 0 of 1500 tokens" in capsys.readouterr().err
    assert ki.main(["preview", "dentists", "--budget", "0"]) == 0
    assert "0 file(s): none; about 0 of 0 tokens" in capsys.readouterr().err
