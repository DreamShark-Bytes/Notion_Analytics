"""Checks on the public documentation (README.md and docs/): links resolve and no internal strings leak."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

# Patterns for private paths and addresses; none belong in files that go to GitHub.
FORBIDDEN = {
    "a Windows user-profile path": re.compile(r"[A-Za-z]:\\Users\\(?!YourName\b|you\b|user\b|username\b)[A-Za-z0-9._-]+"),
    "a Linux home path": re.compile(r"/home/[a-z][\w.-]*"),
    "an email address": re.compile(r"[\w.+-]+@(?!example\.)[\w-]+\.[a-z]{2,}", re.I),
}


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation except - and _, spaces to hyphens."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors, fence = set(), False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            fence = not fence
        elif not fence and (m := re.match(r"#{1,6} (.+)", line)):
            anchors.add(_slug(m.group(1)))
    return anchors


def _links(path: Path):
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", text):
        yield m.group(1)


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(doc):
    problems = []
    for target in _links(doc):
        if re.match(r"[a-z]+:", target):  # http:, https:, mailto:
            continue
        file_part, _, anchor = target.partition("#")
        dest = doc if not file_part else (doc.parent / file_part).resolve()
        if not dest.exists():
            problems.append(f"{target}: file not found")
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            problems.append(f"{target}: no such heading in {dest.name}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_internal_strings(doc):
    text = doc.read_text()
    found = [name for name, pattern in FORBIDDEN.items() if pattern.search(text)]
    assert not found, f"{doc.name} contains internal strings: {found}"


def test_docs_index_lists_every_page():
    index = (ROOT / "docs" / "README.md").read_text()
    missing = [p.name for p in (ROOT / "docs").glob("*.md") if p.name != "README.md" and p.name not in index]
    assert not missing, f"docs/README.md does not link: {missing}"
