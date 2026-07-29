"""Guards on the words the UI actually shows.

Cheap, and they enforce the whole premise of the rework against drift: the
point was that a non-technical person can use this, which stops being true the
moment "max_evals" or "dedup" reappears on screen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import deal_radar.web

STATIC = Path(deal_radar.web.__file__).parent / "static"

# Internal vocabulary that must never reach the screen. Matched against user-
# visible strings only — code identifiers and comments are exempt below.
JARGON = re.compile(
    r"\b(dedup|max[_-]evals|min_rating|notify_top_n|SSE|EventSource|headless|headful|"
    r"storage_state|dry[_-]run|sqlite|pydantic|NotImplementedError|traceback|"
    r"stdout|stderr|localhost:|api_key_env)\b",
    re.IGNORECASE,
)

# Strings that live in code rather than on screen.
CODE_CONTEXT = re.compile(
    r"""^\s*(?://|\*|/\*)"""  # comment lines
    r"""|^\s*import\s|^\s*from\s"""
    r"""|data-loc|dataset\.|classList|querySelector|addEventListener"""
)


def _visible_strings(source: str) -> list[tuple[int, str]]:
    """Quoted strings that plausibly end up in front of a user."""
    out: list[tuple[int, str]] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if CODE_CONTEXT.search(line):
            continue
        for match in re.finditer(r"""(['"`])((?:\\.|(?!\1).){4,})\1""", line):
            text = match.group(2)
            # Skip things that are obviously selectors, URLs, or class lists.
            if text.startswith(("/api/", "/static/", "#", ".", "http")):
                continue
            if " " not in text:
                continue
            out.append((number, text))
    return out


@pytest.mark.parametrize("name", ["index.html", "main.js", "setup.js", "config.js"])
def test_no_internal_jargon_on_screen(name: str) -> None:
    source = (STATIC / name).read_text()
    if name.endswith(".html"):
        # Strip comments, then check everything that renders.
        body = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)
        found = JARGON.search(body)
        assert not found, f"{name} shows {found.group(0)!r}"
        return
    for number, text in _visible_strings(source):
        found = JARGON.search(text)
        assert not found, f"{name}:{number} shows {found.group(0)!r} in {text!r}"


def test_the_old_confusing_labels_are_gone() -> None:
    """These were the specific things a first-timer could not decode."""
    page = (STATIC / "index.html").read_text()
    js = (STATIC / "main.js").read_text()
    for gone in ("Start loop", "Best offers", "Show best", "Live log", "Message drafts"):
        assert gone not in page, f"{gone!r} is still on the page"
    assert "Recent listings" not in page
    # The star and camera glyphs are replaced by named badges.
    assert "★" not in js
    assert "📷" not in js


def test_the_new_labels_are_present() -> None:
    page = (STATIC / "index.html").read_text()
    for wanted in (
        "Keep watching",
        "Deals worth a look",
        "Messages waiting for you",
        "Everything checked recently",
        "Activity",
        "Settings",
    ):
        assert wanted in page, f"{wanted!r} is missing"


def test_the_skipped_label_states_the_real_reason() -> None:
    """The old tooltip said "not fully scraped / not evaluated".

    Tracing the pipeline: a listing only lands in the store with a null rating
    when a *filter* dropped it; an evaluation error never reaches the store at
    all. So the old wording described a state that cannot occur.
    """
    js = (STATIC / "main.js").read_text()
    # Comments may still quote the old wording to explain why it was wrong.
    rendered = "\n".join(
        line for line in js.splitlines() if not line.lstrip().startswith("//")
    )
    assert "not fully scraped" not in rendered
    assert "Skipped" in rendered
    assert "price range" in rendered and "excluded" in rendered


def test_ratings_are_explained_not_just_shown() -> None:
    js = (STATIC / "main.js").read_text()
    assert "of 5" in js, "a bare 4/5 never said what the number meant"
    assert "/5<" not in js


def test_destructive_actions_confirm() -> None:
    js = (STATIC / "main.js").read_text()
    # Both forgetting one listing and forgetting all of them.
    assert js.count("confirm(") >= 2
    assert "turn up again" in js


def test_cost_is_stated_before_the_click() -> None:
    js = (STATIC / "main.js").read_text()
    assert "may cost about" in js
    assert "Usually much less" in js


def test_the_free_scan_is_offered_and_labelled_free() -> None:
    page = (STATIC / "index.html").read_text()
    assert "Test scan (free)" in page
    assert "costs nothing" in page
