"""Scan a repository for text that must not be published.

Usage:
  python scripts/leak_check.py [--repo DIR] [--terms FILE] [--refs REF ...]
                               [--untracked] [--json OUT] [--no-terms]

Scans
  - every tracked file (the working-tree copy of each path in the index), and
    its path; with --untracked, also files git does not ignore but does not
    track yet (to check before a commit);
  - every commit reachable from --refs (default: all branches and tags): the
    message, author and committer names and e-mails, the paths it touches and
    every line it adds (so every line that was ever in the history is seen
    once, in the commit that introduced it).

Built-in checks (generic, safe to publish)
  home_path     an absolute home-directory path
  email         any e-mail address except ALLOWED_EMAILS (and allow-email lines)
  hangul        Korean text (the public files are English)
  private_path  a path that is ignored on purpose (private notes, local data)
                appearing in a tracked file list or in any commit

Private checks come from a terms file that is never tracked (default:
<repo>/private/leak_terms.txt; one "<kind> <regex>" per line, kinds `term`,
`name` and `allow-email`, matched case-insensitively). `term` patterns are
flagged anywhere; `name` patterns feed the check application_near_name (the
word "application" within two lines of a listed name or a title such as
"Dr."). Without the terms file only the generic checks run, and the script
says so and exits with status 2 unless --no-terms is given.

A finding a person has reviewed and accepted can be acknowledged in the same
file with "ack <rule> <path regex> <match regex>" (fields without spaces; the
path is the file path, "message", "author", "committer" or "path <file>",
matched in full). Acknowledged findings are still printed, marked "ack", and
do not count toward the exit status. Commit hashes change in an export, so
acknowledgements never name a commit.

Exit status: 0 no unacknowledged findings, 1 findings, 2 usage error or terms
file missing.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ALLOWED_EMAILS = {
    "294386472+starriet9@users.noreply.github.com",  # public author identity
    "noreply@anthropic.com",                         # Co-Authored-By trailer
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
HOME_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")
HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af\ud7b0-\ud7ff]+")
PRIVATE_PATH = re.compile(
    r"^(?:private/|data/|tools/(?:jdk|dl|synthea|uv-cache|uv-python|gradle-home|micromamba|"
    r"r-env|r-home)/|\.claude/|\.venv/)|^(?:CLAUDE\.md|KICKOFF\.md|env\.local\.sh)$")
APPLICATION = re.compile(r"\bapplication", re.I)
TITLE_NAME = re.compile(r"\b(?:Dr|Prof|Professor)\.?\s+[A-Z][a-z]+")
WINDOW = 2


@dataclass
class Finding:
    rule: str
    where: str          # "file" or "commit"
    commit: str | None  # full sha for commit findings
    part: str           # file path, "message", "author", "committer", "path"
    line: int | None
    match: str
    text: str
    ack: bool = False

    def label(self) -> str:
        loc = self.part + (f":{self.line}" if self.line else "")
        head = f"commit {self.commit[:10]} {loc}" if self.commit else f"file {loc}"
        return f"{'ack ' if self.ack else ''}{head}  [{self.rule}] {self.match!r}  | {self.text}"


@dataclass
class Terms:
    terms: list[re.Pattern]
    names: list[re.Pattern]
    allow: set[str]
    loaded: bool
    acks: list[tuple[str, re.Pattern, re.Pattern]] = None

    def acknowledged(self, f: Finding) -> bool:
        return any(rule == f.rule and p.fullmatch(f.part) and m.fullmatch(f.match)
                   for rule, p, m in self.acks or [])


def load_terms(path: Path | None) -> Terms:
    t = Terms([], [], set(ALLOWED_EMAILS), False, [])
    if path is None or not path.is_file():
        return t
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        kind, _, pat = s.partition(" ")
        pat = pat.strip()
        if kind == "term":
            t.terms.append(re.compile(pat, re.I))
        elif kind == "name":
            t.names.append(re.compile(pat, re.I))
        elif kind == "allow-email":
            t.allow.add(pat.lower())
        elif kind == "ack":
            rule, path_re, match_re = pat.split()
            t.acks.append((rule, re.compile(path_re), re.compile(match_re)))
        else:
            sys.exit(f"{path}:{n}: unknown kind {kind!r}")
    t.loaded = True
    return t


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout


def short(s: str, n: int = 160) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n - 3] + "..."


def scan_lines(lines: list[str], terms: Terms, mk) -> list[Finding]:
    """Content checks on a list of lines; mk(rule, line_index, match, text) -> Finding."""
    out = []
    for i, line in enumerate(lines):
        for m in HOME_PATH.finditer(line):
            out.append(mk("home_path", i, m.group(0), line))
        for m in EMAIL.finditer(line):
            if m.group(0).lower() not in terms.allow:
                out.append(mk("email", i, m.group(0), line))
        for m in HANGUL.finditer(line):
            out.append(mk("hangul", i, m.group(0), line))
            break  # one per line is enough
        for p in terms.terms:
            for m in p.finditer(line):
                out.append(mk("private_term", i, m.group(0), line))
        if APPLICATION.search(line):
            near = lines[max(0, i - WINDOW): i + WINDOW + 1]
            for other in near:
                hit = TITLE_NAME.search(other) or next(
                    (m for p in terms.names if (m := p.search(other))), None)
                if hit:
                    out.append(mk("application_near_name", i, hit.group(0), line))
                    break
    return out


def scan_path(path: str, terms: Terms, mk) -> list[Finding]:
    out = []
    if PRIVATE_PATH.search(path):
        out.append(mk("private_path", None, path, path))
    for f in scan_lines([path], terms, lambda r, i, m, t: mk(r, None, m, t)):
        if f.rule != "application_near_name":
            out.append(f)
    return out


def decode(b: bytes) -> str | None:
    if b"\0" in b[:8192]:
        return None  # binary
    return b.decode("utf-8", errors="replace")


def scan_files(repo: Path, terms: Terms, untracked: bool = False) -> list[Finding]:
    out = []
    extra = ["--others", "--exclude-standard"] if untracked else []
    paths = git(repo, "ls-files", "-z").decode().split("\0")
    if untracked:
        paths += git(repo, "ls-files", "-z", *extra).decode().split("\0")
    for path in paths:
        if not path:
            continue
        def mk(rule, i, m, t, path=path):
            return Finding(rule, "file", None, path, None if i is None else i + 1, m, short(t))
        out += scan_path(path, terms, mk)
        p = repo / path
        if not p.is_file():
            continue
        text = decode(p.read_bytes())
        if text is not None:
            out += scan_lines(text.splitlines(), terms, mk)
    return out


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_commits(repo: Path, refs: list[str], terms: Terms) -> tuple[list[Finding], int]:
    shas = git(repo, "rev-list", "--reverse", *refs).decode().split()
    out = []
    for sha in shas:
        meta = git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha).decode(
            "utf-8", errors="replace")
        an, ae, cn, ce, body = meta.split("\0", 4)
        def mk(rule, i, m, t, part, base=0):
            return Finding(rule, "commit", sha, part, None if i is None else base + i + 1, m,
                           short(t))
        for part, name, email in (("author", an, ae), ("committer", cn, ce)):
            ident = f"{name} <{email}>"
            if email.lower() not in terms.allow:
                out.append(mk("email", None, email, ident, part))
            for f in scan_lines([name], terms, lambda r, i, m, t, part=part: mk(r, None, m, ident,
                                                                                 part)):
                if f.rule != "email":
                    out.append(f)
        out += scan_lines(body.splitlines(), terms,
                          lambda r, i, m, t: mk(r, i, m, t, "message"))
        # Paths touched (including deletions and both sides of renames).
        names = git(repo, "show", "--format=", "--name-status", "-z", "-m", "--no-renames",
                    sha).decode("utf-8", errors="replace").split("\0")
        for p in {x for x in names[1::2] if x}:
            out += scan_path(p, terms, lambda r, i, m, t, p=p: mk(r, None, m, t, f"path {p}"))
        # Added lines with their new-file line numbers.
        patch = git(repo, "show", "--format=", "-p", "-m", "--no-color", "--no-ext-diff",
                    "--unified=0", "--no-renames", sha).decode("utf-8", errors="replace")
        cur, lineno, block, start = None, 0, [], 0
        def flush():
            nonlocal block
            if cur and block:
                out.extend(scan_lines(block, terms, lambda r, i, m, t: mk(
                    r, i, m, t, cur, base=start - 1)))
            block = []
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                flush()
                cur = None
            elif line.startswith("+++ "):
                tgt = line[4:]
                cur = tgt[2:] if tgt.startswith("b/") else None
            elif (h := HUNK.match(line)):
                flush()
                lineno = start = int(h.group(1))
            elif line.startswith("+") and cur is not None:
                if not block:
                    start = lineno
                block.append(line[1:])
                lineno += 1
        flush()
    return out, len(shas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--terms", type=Path, help="private terms file "
                    "(default: <repo>/private/leak_terms.txt)")
    ap.add_argument("--refs", nargs="+", default=["--branches", "--tags"],
                    help="revisions to scan, as for git rev-list (default: all branches "
                    "and tags; write options as --refs=--all)")
    ap.add_argument("--untracked", action="store_true",
                    help="also scan files that are neither tracked nor ignored")
    ap.add_argument("--no-terms", action="store_true",
                    help="run without a terms file (generic checks only)")
    ap.add_argument("--json", type=Path, help="write all findings to this file")
    a = ap.parse_args()

    repo = a.repo.resolve()
    terms = load_terms(a.terms or repo / "private" / "leak_terms.txt")
    if not terms.loaded:
        print("leak_check: private terms file not found; only generic checks run",
              file=sys.stderr)
        if not a.no_terms:
            return 2
    files = scan_files(repo, terms, a.untracked)
    commits, n_commits = scan_commits(repo, a.refs, terms)
    findings = files + commits
    by_rule: dict[str, int] = {}
    for f in findings:
        f.ack = terms.acknowledged(f)
        print(f.label())
        if not f.ack:
            by_rule[f"{f.where}:{f.rule}"] = by_rule.get(f"{f.where}:{f.rule}", 0) + 1
    open_ = [f for f in findings if not f.ack]
    summary = {"repo_head": git(repo, "rev-parse", "HEAD").decode().strip(),
               "commits_scanned": n_commits,
               "tracked_files": len([p for p in git(repo, "ls-files", "-z").split(b"\0") if p]),
               "terms_loaded": terms.loaded, "findings": len(open_),
               "acknowledged": len(findings) - len(open_), "by_rule": by_rule}
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    if a.json:
        a.json.write_text(json.dumps({"summary": summary,
                                      "findings": [asdict(f) for f in findings]},
                                     indent=1, ensure_ascii=False) + "\n")
    return 1 if open_ else 0


if __name__ == "__main__":
    sys.exit(main())
