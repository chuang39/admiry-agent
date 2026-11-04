"""Self-hosted site fonts (A3p-6 batch 1 Task 3 part 1; plan Q16, Q17): every face a style names has its file, each file
is a woff2, each face directory carries its OFL.txt, and a style's fonts stay inside the 220 KB budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.sites import families

FONTS = Path(__file__).resolve().parents[1] / "data" / "sites" / "fonts"
STYLES = [(f, n, s) for f in sorted(families.FAMILIES) for n, s in families.get(f).styles.items()]
IDS = [f"{f}/{n}" for f, n, _ in STYLES]


@pytest.mark.parametrize(("family", "name", "style"), STYLES, ids=IDS)
def test_every_face_a_style_names_has_its_file(family, name, style):
    assert style.fonts
    for ff in style.fonts:
        p = FONTS / ff.file
        assert "[" not in ff.file and "]" not in ff.file, ff.file
        assert p.is_file(), ff.file
        assert p.read_bytes()[:4] == b"wOF2", ff.file
        assert (p.parent / "OFL.txt").is_file(), ff.file


@pytest.mark.parametrize(("family", "name", "style"), STYLES, ids=IDS)
def test_a_styles_fonts_fit_the_budget(family, name, style):
    total = sum((FONTS / ff.file).stat().st_size for ff in {f.file: f for f in style.fonts}.values())
    assert total <= 220 * 1024, f"{total} bytes"


def test_sources_records_every_file():
    text = (FONTS / "SOURCES.md").read_text()
    for p in FONTS.glob("*/*.woff2"):
        assert p.name in text, p.name
