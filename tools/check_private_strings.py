"""Block private strings in staged changes: user-profile and home paths, email addresses, real-looking
Notion/UUID ids, and any extra terms you configure.

Only the lines a commit ADDS are checked, so older files are not re-flagged. A line containing
`secret-scan: allow` is skipped (the marker the shared secret scanner already uses).

Extra terms (host names, your own name) are not stored in the repository: set them in this clone's
git config, comma separated:

    git config secretscan.internalterms "term1,term2"

Install as this clone's pre-commit hook (hooks are not versioned, so repeat on every clone; the shared
hook in ~/.ai/git-hooks runs this local hook after its own scan):

    printf '#!/usr/bin/env bash\\nexec python3 tools/check_private_strings.py\\n' > .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
"""
from __future__ import annotations

import re
import subprocess
import sys

ALLOW_MARKER = "secret-scan: allow"
PLACEHOLDER = r"(?:YourName|yourname|you|user|username|name)\b"

PATTERNS = [
    ("user-profile path", re.compile(rf"[A-Za-z]:[\\/]+Users[\\/]+(?!{PLACEHOLDER})(?!<)[\w.-]+")),
    ("home path", re.compile(rf"/home/(?!{PLACEHOLDER})[a-z][\w.-]*")),
    ("email address", re.compile(r"[\w.+-]+@(?!users\.noreply\.github\.com|example\.|noreply\.)[\w-]+(?:\.[\w-]+)+")),
]
ID_PATTERN = re.compile(r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b", re.I)


def _is_fake_id(value: str) -> bool:
    """Test fixtures like aaaaaaaa-0000-0000-0000-000000000001 start with one repeated character."""
    return bool(re.fullmatch(r"(.)\1{7}", value.replace("-", "")[:8]))


def find_problems(diff_text: str, extra_terms: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Return (path, kind, matched text) for every added line that holds a private string."""
    terms = [t.lower() for t in (extra_terms or []) if t]
    problems, path = [], "?"
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else "?"
        elif line.startswith("+") and ALLOW_MARKER not in line:
            text = line[1:]
            for kind, pattern in PATTERNS:
                if match := pattern.search(text):
                    problems.append((path, kind, match.group(0)))
            for match in ID_PATTERN.finditer(text):
                if not _is_fake_id(match.group(0)):
                    problems.append((path, "id (Notion page or database id?)", match.group(0)))
            lowered = text.lower()
            problems += [(path, "configured term", term) for term in terms if term in lowered]
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def main() -> int:
    diff = _git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR")
    terms = [t.strip() for t in _git("config", "--get", "secretscan.internalterms").split(",")]
    problems = find_problems(diff, terms)
    if not problems:
        return 0
    print("check_private_strings: BLOCKED. This commit adds private strings:", file=sys.stderr)
    for path, kind, shown in dict.fromkeys(problems):
        print(f"  {path}: {kind} [{shown}]", file=sys.stderr)
    print("Move the value to an environment variable, an untracked file or git config, or use a placeholder.\n"
          f"A line you really mean to publish can carry '{ALLOW_MARKER}'.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
