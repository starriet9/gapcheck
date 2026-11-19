"""Build the project page (pages/index.html) from repository files.

Usage:
  python scripts/site_build.py [--out pages] [--strict]

Inputs (all in the repository; nothing is fetched):
  site/config.json      title, subtitle, links, which limits to show
  site/user_text.md     sentences the author writes (placeholders until then)
  site/log.json         the dated log (at most 10 rows)
  site/events.json      public upstream events with server timestamps
  site/data/card.json   the twin-patient card (scripts/site_card.py)
  site/data/score.json  score file of the pre-registered test run (written by
                        scripts/score_runs.py; aggregate numbers only); if it is
                        absent the page is in its preview state
  SPEC.md, PROTOCOL.md, RUNS.md, Makefile, git history

Output: one self-contained HTML file (CSS and JS inline, no external requests)
plus the images it or the README refer to. The page has no build timestamp,
so the same inputs give the same bytes.

--strict (use before publishing) stops if an author placeholder is left, a
log row is unresolved, or a listed file is missing from the repository.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
PROBLEMS: list[str] = []  # reported at the end; fatal with --strict


def problem(msg: str) -> None:
    PROBLEMS.append(msg)


# ---------------------------------------------------------------- small helpers
def e(x) -> str:
    return escape(str(x), quote=True)


def inline_md(text: str) -> str:
    """Escape, then `code`, **bold**, *em*, [text](url)."""
    t = escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?!\w)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def autolink_refs(html: str) -> str:
    html = re.sub(r"arXiv (\d{4}\.\d{4,5})", r'<a href="https://arxiv.org/abs/\1">arXiv \1</a>', html)
    html = re.sub(r"doi:(10\.\d{4,9}/[^\s,;)]+[^\s,;).])", r'<a href="https://doi.org/\1">doi:\1</a>', html)
    return html


def slug(heading: str) -> str:
    """GitHub's heading anchor."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def nice_date(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          check=True).stdout


def read(rel: str) -> str | None:
    p = ROOT / rel
    return p.read_text() if p.exists() else None


class Repo:
    def __init__(self, cfg: dict):
        self.url = cfg["repo_url"].rstrip("/")
        self.branch = cfg["branch"]
        self.log = []
        for ln in git("log", "--reverse", "--format=%H%x09%aI%x09%s").splitlines():
            sha, when, subj = ln.split("\t", 2)
            self.log.append((sha, parse_ts(when), subj))

    def blob(self, path: str, anchor: str = "") -> str:
        kind = "tree" if path.endswith("/") else "blob"
        return f"{self.url}/{kind}/{self.branch}/{path.rstrip('/')}" + (f"#{anchor}" if anchor else "")

    def commit_url(self, sha: str) -> str:
        return f"{self.url}/commit/{sha}"

    def first_commit(self) -> tuple[str, datetime]:
        sha, when, _ = self.log[0]
        return sha, when

    def find(self, prefix: str) -> tuple[str, datetime] | None:
        hits = [(s, w) for s, w, subj in self.log if subj.startswith(prefix)]
        if len(hits) != 1:
            problem(f"log: commit subject {prefix!r} matched {len(hits)} commits")
            return hits[0] if hits else None
        return hits[0]


def tracked(path: str) -> bool:
    """In git (or, for a directory, has tracked files)."""
    out = git("ls-files", "--", path.rstrip("/"))
    return bool(out.strip())


# ---------------------------------------------------------------- repo documents
def md_section(text: str, number: str) -> tuple[str, str]:
    """(heading, body) of the '## <number>. ...' section of a markdown file."""
    m = re.search(rf"^## ({re.escape(number)}\. [^\n]*)\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit(f"section {number} not found")
    return m.group(1), m.group(2)


def spec_limits(spec: str, picks: list[str]) -> tuple[list[str], str]:
    head, body = md_section(spec, "11")
    para = " ".join(x.strip() for x in body.strip().splitlines() if x.strip())
    items = [x.strip().rstrip(".") for x in re.split(r";\s+(?=[a-z\"A-Z])", para)]
    out = []
    for k in picks:
        hit = [x for x in items if k.lower() in x.lower()]
        if len(hit) != 1:
            raise SystemExit(f"limits: {k!r} matched {len(hit)} items in SPEC sec. 11")
        out.append(hit[0][0].upper() + hit[0][1:] + ".")
    return out, slug(head)


def spec_conditions(spec: str) -> dict[str, dict]:
    out = {}
    for ln in spec.splitlines():
        m = re.match(r"\|\s*(C\d[ab]?) ([^|]+?)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|", ln)
        if m:
            out[m.group(1)] = {"name": m.group(2), "edit": m.group(3).strip(),
                               "applied": m.group(4).strip(), "correct": m.group(5).strip(),
                               "tests": m.group(6).strip()}
    return out


def spec_systems(spec: str) -> list[tuple[str, str]]:
    _, body = md_section(spec, "6")
    out = []
    for ln in body.splitlines():
        m = re.match(r"\|\s*(R0|R1|L1|A1|A2)\s*\|\s*([^|]+?)\s*\|", ln)
        if m:
            first = re.split(r"(?<=\.)\s", m.group(2))[0]
            out.append((m.group(1), first))
    return out


def hypotheses(spec: str, protocol: str | None) -> tuple[list[str], str]:
    if protocol:
        hs = re.findall(r"^- \*\*(H\d)\.\*\* (.+)$", protocol, re.M)
        if hs:
            return [f"**{h}.** {t}" for h, t in hs], "PROTOCOL.md"
    hs = re.findall(r"^- (H\d): (.+)$", spec, re.M)
    if not hs:
        raise SystemExit("no hypotheses found in PROTOCOL.md or SPEC.md")
    return [f"**{h}.** {t}" for h, t in hs], "SPEC.md"


def expected_result(spec: str) -> str:
    m = re.search(r"^\*\*Expected result\.\*\* (.+)$", spec, re.M)
    if not m:
        raise SystemExit("SPEC.md: no 'Expected result' paragraph")
    return m.group(1)


def planned_cases(spec: str) -> str:
    m = re.search(r"about (\d[\d,]*) test cases", spec)
    if not m:
        raise SystemExit("SPEC.md: no 'about N test cases'")
    return m.group(1)


def prior_work(spec: str) -> tuple[list[str], str, str]:
    head, body = md_section(spec, "2")
    bullets = re.findall(r"^- (.+)$", body, re.M)
    ext = re.search(r"^(This project extends[^\n]*?\.)(?: |$)", body, re.M)
    first = body.strip().splitlines()[0].strip()
    if not bullets or not ext:
        raise SystemExit("SPEC.md sec. 2: prior-work list or 'This project extends' sentence not found")
    return bullets, first + " " + ext.group(1), slug(head)


def runs_md(text: str | None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(two-column field table values, rows of the runs table by run number)."""
    fields, runs = {}, {}
    if not text:
        return fields, runs
    for ln in text.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if not ln.startswith("|") or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) == 2 and cells[0] not in ("Field",):
            fields[cells[0]] = cells[1]
        elif len(cells) > 2 and re.fullmatch(r"\d+", cells[0]):
            runs[cells[0]] = cells
    return fields, runs


def is_pending(v: str | None) -> bool:
    return v is None or v.strip().lower().startswith("pending") or v.strip() in ("", "–", "-")


def field(fields: dict[str, str], key: str) -> str | None:
    hits = [v for k, v in fields.items() if key.lower() in k.lower()]
    return hits[0] if hits else None


def make_targets(text: str | None) -> set[str]:
    return set(re.findall(r"^([a-z][\w-]*):", text or "", re.M))


def user_sections(text: str) -> dict[str, str]:
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S)
    out = {}
    for m in re.finditer(r"^## (\w+)\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


def user_block(sections: dict[str, str], key: str, tag: str = "div") -> str:
    body = sections.get(key, "")
    hint = re.fullmatch(r"<!--\s*USER:\s*(.*?)\s*-->", body, re.S)
    if hint or not body:
        h = hint.group(1) if hint else key
        problem(f"author placeholder left: site/user_text.md ## {key}")
        return f'<!-- USER: {e(h)} --><{tag} class="user">{e(h)}</{tag}>'
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return "".join(f"<p>{inline_md(' '.join(p.split()))}</p>" for p in paras)


# ---------------------------------------------------------------- card
STATUS = {"GAP": "GAP", "CANNOT_DETERMINE": "CANNOT DETERMINE", "DOCUMENTED_REASON": "DOCUMENTED REASON",
          "NOT_INDICATED": "NOT INDICATED", "ON_THERAPY": "ON THERAPY", "DISCUSS": "DISCUSS"}
SEX = {"M": "man", "F": "woman"}


def short_id(x: str) -> str:
    return x if len(x) <= 12 else x[:10] + "…"


def src_text(ln: dict) -> str:
    s = ln.get("source")
    if isinstance(s, list):
        ids = [x for x in s if x != "demographics"]
        if not ids:
            return "demographics"
        more = f" +{len(ids) - 1}" if len(ids) > 1 else ""
        return f'<span title="{e(", ".join(ids))}">{e(short_id(ids[0]))}{more}</span>'
    if ln.get("support") == "ABSENT" and s:
        return f"checked: {e(s)}"
    return e(s or "")


def line_html(ln: dict, deleted_note: str = "") -> str:
    if ln.get("value") is None:
        return ""
    cls = " deleted" if ln.get("deleted") else (" blocked" if ln.get("blocked") else "")
    lab = f'<span class="lab">{e(ln["label"])}</span> '
    if ln.get("deleted"):
        meta = f'<strong>deleted</strong>{e(deleted_note)} · was {src_text(ln)}'
        val = (f'<div>{lab}<del><span class="val">{e(ln["value"])}</span></del>'
               f'<span class="sr"> (deleted)</span></div>')
    elif ln.get("blocked"):
        meta = "no lipid panel, so PREVENT has no input" if ln["name"] == "risk_10y" else ""
        val = f'<div>{lab}<span class="val">{e(ln["value"])}</span></div>'
    else:
        parts = [f'<span class="tag">{e(ln["support"])}</span>', src_text(ln)]
        if ln.get("date"):
            parts.append(e(ln["date"]))
        meta = " · ".join(p for p in parts if p)
        val = f'<div>{lab}<span class="val">{e(ln["value"])}</span></div>'
    return f'<li class="{cls.strip()}">{val}<div class="meta">{meta}</div></li>' 


def pill(status: str, extra: str = "") -> str:
    return f'<span class="pill {e(status)}{extra}">{e(STATUS.get(status, status))}</span>'


def agent_line(a: dict | None) -> str:
    if not a:
        return '<p class="agent">Agent on this record: not run yet.</p>'
    same = "same as the answer key" if a["same_as_answer_key"] else "differs from the answer key"
    row = f', {e(a["row_text"])}' if a.get("row_text") else ""
    miss = f'; missing: {e(", ".join(a["missing"]))}' if a.get("missing") else ""
    rounds = a.get("verifier_rounds") or []
    ver = ""
    if rounds:
        ver = "; verifier: " + ("all checks passed" if rounds[0]["ok"] else
                                f"check {', '.join(map(str, rounds[0]['failed_checks']))} failed, then "
                                + ("repaired" if rounds[-1]["ok"] else "forced to CANNOT DETERMINE"))
    return (f'<p class="agent">Agent ({e(a["system"])}, development run) on this record: '
            f'<strong>{e(STATUS.get(a["status"], a["status"]))}</strong>{row}{miss}{ver} ({same}).</p>')


def who(p: dict, prefix: str = "Synthetic patient") -> str:
    return f'{prefix}, {SEX.get(p["sex"], p["sex"])}, {p["age"]}'


def card_html(card: dict) -> str:
    st = card["states"]
    s1, s2, s3 = st["original"], st["edited"], st["caught"]
    names = {"lipid_panel": "Lipid panel", "sbp": "Systolic BP", "smoking": "Smoking status"}
    deleted_name = names.get(s2["variant"], s2["variant"])
    tabs = [("original", "Original record"), ("edited", f"{deleted_name} deleted")]
    if s3:
        tabs.append(("caught", "Caught by the verifier"))

    def all_lines(s, title):
        rows = "".join(line_html(x) for x in s["all_lines"])
        return f'<details class="all"><summary>{e(title)} ({len(s["all_lines"])})</summary><ul class="lines">{rows}</ul></details>'

    # state 1
    k1 = s1["answer_key"]
    p1 = (f'<p class="who">{e(who(s1["patient"]))}. Original record.</p>'
          f'<div class="verdict"><span class="k">Answer key</span>{pill(k1["status"])}'
          f'<span class="row">{e(k1["row_text"] or "")}</span></div>'
          f'<p class="sec">What matters</p><ul class="lines">'
          + "".join(line_html(x) for x in s1["lines"]) + "</ul>"
          '<p class="sec">What\'s missing</p><div class="missing"><p>Nothing that decides the answer.</p></div>'
          + agent_line(s1["agent"]) + all_lines(s1, "Every input the answer key read"))
    # state 2
    k2 = s2["answer_key"]
    note = f" ({s2['deleted_rows']} rows)" if s2["deleted_rows"] else ""
    notes = (f", and its mentions in {s2['scrubbed_notes']} notes" if s2["scrubbed_notes"] else "")
    miss = "".join(f"<p>{e(t)}</p>" for t in s2["missing_text"]) or "<p>—</p>"
    p2 = (f'<p class="who">Same patient. Every {e(deleted_name.lower())} result deleted: '
          f'{s2["deleted_rows"]} record rows{e(notes)}.</p>'
          f'<div class="verdict"><span class="k">Answer key</span>{pill(k2["status"])}'
          f'<span class="row">missing: {e(", ".join(m.replace("_", " ") for m in k2["missing"]))}</span>'
          f'<span class="was">Original record: {e(STATUS[k1["status"]])}</span></div>'
          f'<p class="sec">What matters</p><ul class="lines">'
          + "".join(line_html(x, note) for x in s2["lines"]) + "</ul>"
          f'<p class="sec">What\'s missing</p><div class="missing">{miss}</div>'
          + agent_line(s2["agent"]))
    panels = [p1, p2]
    dwell = [3000, 3000]
    # state 3
    if s3:
        f = s3["first"]
        plant = s3.get("planted") or []
        edit = (f' Edit {e(s3["condition"])} (must not change the answer): a note now says '
                f'&ldquo;{e(plant[0]["text"])}&rdquo;' if plant else "")
        cites = " · ".join(
            f'{e(x["label"])} {e(x["support"])}'
            + (f' ({len(x["source"])} rows)' if isinstance(x["source"], list) and len(x["source"]) > 1 else "")
            for x in f["lines"])
        # one block per (check, kind); the names of the failed claims listed together
        groups: dict[tuple, list] = {}
        for x in s3["failed"]:
            groups.setdefault((x["check"], x["kind"]), []).append(x)
        fails = ""
        for (chk, kind), xs in groups.items():
            what = "Listed as missing" if kind == "missing" else "Claim"
            names = ", ".join(x["label"] for x in xs)
            n = len(xs)
            found = all("is on record" in x["detail"] for x in xs)
            tail = (": each is on record" if n > 1 else ": it is on record") if found else ""
            fails += (f'<div class="fail">{what}: <strong>{e(names)}</strong>'
                      f'<span class="small">Verifier check {chk} failed{" for all " + str(n) if n > 1 else ""}'
                      f'{e(tail)}. Check {chk}: {e(card["checks"][str(chk)])}.</span></div>')
        r = s3["repaired"]
        k3 = s3["answer_key"]
        model = s3["run"]["model_display"]
        rep = "".join(
            f'<li><span class="lab">{e(x["label"])}</span> <span class="val">{e(x["value"])}</span> '
            f'<span class="meta">{e(x["support"])} · {src_text(x)}</span></li>' for x in r["lines"])
        p3 = (f'<p class="who">{e(who(s3["patient"], "Another synthetic patient"))}.{edit}</p>'
              f'<div class="verdict"><span class="k">Agent&rsquo;s first answer</span>{pill(f["status"])}'
              f'<span class="row">{e(f["row_text"] or "")}</span></div>'
              f'<p class="small cites">Cites: {cites}</p>{fails}'
              f'<p class="step">The agent is told only the check number and the claim, and gets one '
              f'repair round.</p>'
              f'<div class="verdict"><span class="k">After repair</span>{pill(r["status"])}'
              f'<span class="row">{e(r["row_text"] or "")}</span></div>'
              f'<ul class="lines tight">{rep}</ul>'
              f'<p class="agent">{"All checks pass" if r["all_checks_pass"] else "Checks still fail"} '
              f'({r["n_claims"]} claims). Answer key: <strong>{e(STATUS[k3["status"]])}</strong>, '
              f'{e(k3["row_text"] or "")}.</p>'
              f'<p class="agent">{e(card["label"])} Model: {e(model)}, temperature '
              f'{e(s3["run"]["temperature"])}.</p>')
        panels.append(p3)
        dwell.append(7000)
    tab_html = "".join(
        f'<button type="button" role="tab" class="tab" id="tab-{k}" aria-controls="state-{k}" '
        f'aria-selected="{"true" if n == 0 else "false"}" tabindex="{0 if n == 0 else -1}">{e(lab)}</button>'
        for n, (k, lab) in enumerate(tabs))
    panel_html = "".join(
        f'<section class="state" id="state-{k}" role="tabpanel" aria-labelledby="tab-{k}" '
        f'data-dwell="{dwell[n]}"><h3 class="state-title">{n + 1}. {e(lab)}</h3>{panels[n]}</section>'
        for n, (k, lab) in enumerate(tabs))
    rules = "".join(f"<li>{e(v)}</li>" for v in card["rules"].values())
    return (f'<div class="card" id="card" aria-label="Example: one synthetic patient, before and after an edit">'
            f'<div class="tabs" role="tablist" aria-label="Card states">{tab_html}</div>'
            f'{panel_html}'
            f'<div class="card-note"><div class="note-row"><span>Development case set '
            f'{e(card["provenance"]["case_set"])}, not the test set.</span>'
            f'<button type="button" class="pause" aria-pressed="false">Pause</button></div>'
            f'<details><summary>How these examples were picked</summary><ul class="plain">{rules}</ul></details></div>'
            f'</div>')


# ---------------------------------------------------------------- page parts
def strip_html(cfg: dict, repo: Repo, fields: dict, runs: dict) -> tuple[str, bool]:
    items = []
    released = False
    for st in cfg["strip"]:
        when, url = None, None
        if st["from"] == "git":
            sha, when = repo.first_commit()
            url = repo.commit_url(sha)
        elif "run_row" in st:
            row = runs.get(st["run_row"])
            fin = row[5] if row and len(row) > 5 else None
            if not is_pending(fin):
                when = parse_ts(fin) if "T" in fin else datetime.fromisoformat(fin[:10])
                url = repo.blob("RUNS.md", st.get("anchor", ""))
        else:
            v = field(fields, st["date_field"])
            if not is_pending(v):
                when = parse_ts(v.strip("` "))
                u = field(fields, st["url_field"])
                url = u.strip("<>` ") if u and not is_pending(u) else repo.blob("RUNS.md")
        if st["key"] == "protocol" and when:
            released = True
        lab = st["label"]
        if when:
            items.append(f'<li class="done"><span class="dot" aria-hidden="true"></span>'
                         f'<a href="{e(url)}">{e(lab[0].upper() + lab[1:] if not items else lab)} '
                         f'<time datetime="{when.date().isoformat()}">{nice_date(when.date())}</time></a></li>')
        else:
            items.append(f'<li class="pend"><span class="dot" aria-hidden="true"></span>'
                         f'{e(lab)}: pending</li>')
    note = ("" if released else
            '<p class="strip-note">Protocol: draft, not yet frozen. Dates are UTC; each links to its record.</p>')
    if released:
        note = '<p class="strip-note">Dates are UTC; each links to its record.</p>'
    return f'<ol class="strip" aria-label="Timeline">{"".join(items)}</ol>{note}', released


def log_html(log: dict, events: dict, repo: Repo, cfg: dict) -> str:
    rows = []
    for n, r in enumerate(log["rows"]):
        when, links, stamp = None, [], ""
        for ln in r.get("links", []):
            if "commit" in ln:
                hit = repo.find(ln["commit"])
                if hit:
                    links.append(f'<a href="{e(repo.commit_url(hit[0]))}">{e(ln["label"])}</a>')
                    when = when or hit[1]
            elif "event" in ln:
                ev = events.get(ln["event"])
                if not ev:
                    problem(f"log: unknown event {ln['event']}")
                    continue
                t = parse_ts(ev["created_at"])
                links.append(f'<a href="{e(ev["url"])}">{e(ln["label"])}</a>')
                stamp = f' <span class="small">({t.strftime("%H:%M")} UTC, GitHub)</span>'
                when = t
            elif "path" in ln:
                if not tracked(ln["path"]):
                    problem(f"log: {ln['path']} is not tracked")
                links.append(f'<a href="{e(repo.blob(ln["path"]))}">{e(ln["label"])}</a>')
        if r.get("date"):
            typed = date.fromisoformat(r["date"])
            if when and when.date() != typed:
                problem(f"log row {n + 1}: typed date {typed} differs from its record {when.date()}")
            when = when or datetime(typed.year, typed.month, typed.day, tzinfo=timezone.utc)
        if "user" in r:
            problem(f"author placeholder left: site/log.json row {n + 1}")
            text = f'<!-- USER: {e(r["user"])} --><div class="user">{e(r["user"])}</div>'
        else:
            text = inline_md(r["text"])
        rows.append((when, n, text, links, stamp))
    if len(rows) > 10:
        raise SystemExit(f"site/log.json has {len(rows)} rows; the page shows at most 10")
    far = datetime(9999, 1, 1, tzinfo=timezone.utc)
    rows.sort(key=lambda x: (x[0] or far, x[1]))
    trs = []
    for when, _, text, links, stamp in rows:
        d = (f'<time datetime="{when.date().isoformat()}">{nice_date(when.date())}</time>' if when else "&mdash;")
        ls = (" " + " · ".join(links)) if links else ""
        trs.append(f'<tr><td class="d">{d}</td><td>{text}{ls}{stamp}</td></tr>')
    return (f'<table class="log"><caption class="sr">Dated log, UTC</caption><tbody>{"".join(trs)}</tbody></table>'
            f'<p class="disclose">{e(cfg["ai_disclosure"])}</p>')


def upstream_html(events: dict, me: str) -> str:
    """One item per upstream issue or PR, with each dated event under it.

    Labels come from the event data: the kind, and who wrote it (`user`,
    `issue_user` from the GitHub API). A comment by the issue's author on an
    issue opened by someone else is labelled as a reply from the issue author.
    """
    groups: dict[tuple, list] = {}
    for ev in sorted(events.values(), key=lambda x: x["created_at"]):
        groups.setdefault((ev["repo"], ev["number"]), []).append(ev)
    items = []
    for (repo, num), evs in sorted(groups.items(), key=lambda kv: kv[1][0]["created_at"]):
        lines = []
        for ev in evs:
            t = parse_ts(ev["created_at"])
            if ev["kind"] == "pull_request":
                kind = "Pull request opened"
            elif ev.get("user") and ev.get("user") == ev.get("issue_user") and ev["user"] != me:
                kind = "Reply from the issue author"
            else:
                kind = "Comment"
            stamp = f'{nice_date(t.date())} {t.strftime("%H:%M")} UTC'
            lines.append(f'<a href="{e(ev["url"])}">{kind}</a>, {stamp}')
        head = evs[0]
        state = "merged" if any(x.get("merged_at") for x in evs) else head.get("state")
        chk = max((x["checked_at"] for x in evs if x.get("checked_at")), default=None)
        kind = "pull request" if head["kind"] == "pull_request" else "issue"
        status = f" · {kind} {state}, checked {nice_date(date.fromisoformat(chk))}" if chk else ""
        items.append(f'<li><strong>{e(repo)}#{num}</strong>{e(status)}'
                     f'<br><span class="small">{e(head["title"])}</span>'
                     f'<br>{"<br>".join(lines)}</li>')
    return f'<ul class="plain">{"".join(items)}</ul>'


def conditions_html(conds: dict) -> str:
    groups = (("Edits that must change the answer", ("C1", "C3a", "C5")),
              ("Edits that must not change it", ("C2a", "C2b", "C3b", "C4")))
    out = []
    for title, ids in groups:
        lis = "".join(f'<li><strong>{c} {e(conds[c]["name"])}</strong> &rarr; '
                      f'{inline_md(conds[c]["correct"])}. <span class="small">Tests: '
                      f'{inline_md(conds[c]["tests"][0].lower() + conds[c]["tests"][1:])}.</span></li>'
                      for c in ids)
        out.append(f"<h3>{e(title)}</h3><ul class=\"plain\">{lis}</ul>")
    c0 = conds["C0"]
    det = "".join(f'<li><strong>{c} {e(conds[c]["name"])}.</strong> {inline_md(conds[c]["edit"])}. '
                  f'<span class="small">Applied to: {inline_md(conds[c]["applied"])}.</span></li>'
                  for c in ("C1", "C2a", "C2b", "C3a", "C3b", "C4", "C5"))
    return (f'<p class="small"><strong>C0 {e(c0["name"])}</strong>: no edit; correct is the answer-key label.</p>'
            + "".join(out) + '<details class="all"><summary>What each edit does</summary>'
            f'<ul class="plain">{det}</ul></details>')


def files_html(cfg: dict, repo: Repo) -> str:
    lis = []
    for f in cfg["files"]:
        if tracked(f["path"]):
            lis.append(f'<li><a href="{e(repo.blob(f["path"]))}">{e(f["label"])}</a></li>')
        else:
            if not f.get("pending_text"):
                problem(f"files: {f['path']} is not in the repository yet")
            lis.append(f'<li>{e(f["label"])} <span class="pend">({e(f.get("pending_text", "pending"))})</span></li>')
    return f'<ul class="plain files">{"".join(lis)}</ul>'


def signed(x: float, nd: int = 1) -> str:
    return f"{x:+.{nd}f}".replace("-", "\u2212")


def ci_text(ci, nd: int = 1) -> str:
    lo, hi = ci
    if lo is None:
        return "no interval"
    return f"95% CI {signed(100 * lo, nd)} to {signed(100 * hi, nd)}"


def results_block(res: dict, user: dict, fig_svg: str) -> str:
    """Result section (results state). Numbers come only from the score file: its
    `headline` block (PROTOCOL sec. 10, written by scripts/score_runs.py)."""
    head = res.get("headline")
    if not head:
        raise SystemExit("score file has no 'headline' block: re-score with scripts/score_runs.py")
    tiles = []
    p = head.get("primary")
    if p:
        d = p["difference"]
        tiles.append(f'<li><strong>A2 &minus; L1: {signed(100 * d["est"])} points</strong> '
                     f'({ci_text(d["ci"])}) in macro-averaged exact-status accuracy; '
                     f'A2 {100 * p["A2"]["est"]:.1f}%, L1 {100 * p["L1"]["est"]:.1f}%.</li>')
    else:
        problem("results: no primary outcome (A2 or L1 missing from the score file)")
    for key, lab in (("answered_anyway", "Answered anyway with the deciding input deleted (C1)"),
                     ("changed_when_must_not", "Changed when it must not (C2a, C2b, C3b, C4)")):
        parts = [f'{sy} {v["k"]} of {v["n"]}' for sy, v in head.get(key, {}).items()]
        if parts:
            tiles.append(f"<li>{e(lab)}: {e('; '.join(parts))}.</li>")
        else:
            problem(f"results: headline {key} is empty")
    return (f'<ul class="plain">{"".join(tiles)}</ul>'
            f'<figure>{fig_svg}<figcaption>Figure 1. Percent correct by condition and system, '
            f'95% cluster-bootstrap intervals. Open squares: correct by construction.</figcaption></figure>'
            + user_block(user, "result_sentences"))


def load_figure1():
    spec = importlib.util.spec_from_file_location("figure1", ROOT / "scripts" / "figure1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- page
def build(out_dir: Path, res_p: Path | None = None) -> Path:
    cfg = json.loads((SITE / "config.json").read_text())
    card = json.loads((SITE / "data" / "card.json").read_text())
    log = json.loads((SITE / "log.json").read_text())
    events = {x["id"]: x for x in json.loads((SITE / "events.json").read_text())["events"]}
    user = user_sections((SITE / "user_text.md").read_text())
    res_p = res_p or SITE / "data" / "score.json"
    results = json.loads(res_p.read_text()) if res_p.exists() else None
    spec, protocol, runs_text = read("SPEC.md"), read("PROTOCOL.md"), read("RUNS.md")
    repo = Repo(cfg)
    fields, runs = runs_md(runs_text)
    strip, released = strip_html(cfg, repo, fields, runs)
    fig1 = load_figure1()

    n_cases = (str(results["n_cases"]) if results else planned_cases(spec))
    subtitle = cfg["subtitle"].format(planned="" if released else "planned ", n_cases=n_cases)
    hyps, hyp_src = hypotheses(spec, protocol)
    limits, limits_anchor = spec_limits(spec, cfg["limits_pick"])
    conds = spec_conditions(spec)
    systems = spec_systems(spec)
    refs, prior, prior_anchor = prior_work(spec)
    targets = make_targets(read("Makefile"))
    has = lambda p: tracked(p)  # noqa: E731

    proto_label = "Protocol" if released else "Protocol (draft)"
    nav = [f'<a href="{e(repo.blob("PROTOCOL.md"))}">{proto_label}</a>' if has("PROTOCOL.md") else
           f'<a href="{e(repo.blob("SPEC.md"))}">Design</a>',
           f'<a href="{e(repo.url)}">Code</a>']
    if has(cfg["summary_pdf"]):
        nav.append(f'<a href="{e(repo.blob(cfg["summary_pdf"]))}">One-page summary (PDF)</a>')

    hook = (f'<h1>{e(cfg["title"])}</h1><p class="sub">{e(subtitle)}</p>{strip}'
            f'{card_html(card)}<p class="under">{e(card["footer"])}</p>')

    found = (f'<section class="s" id="found" aria-labelledby="h-found"><h2 id="h-found">Found on the way</h2>'
             f'{user_block(user, "found_on_the_way")}{upstream_html(events, repo.url.rstrip("/").split("/")[-2])}</section>')

    if results:
        svg = fig1.render(results, inline=True)
        result = (f'<section class="s" id="result" aria-labelledby="h-result"><h2 id="h-result">Result</h2>'
                  f'{results_block(results, user, svg)}</section>')
    else:
        svg = fig1.render(None, inline=True)
        src = (f'<a href="{e(repo.blob(hyp_src))}">{e(hyp_src)}</a>'
               + ("" if released or hyp_src != "PROTOCOL.md" else " (draft, not yet frozen)"))
        result = (f'<section class="s" id="predictions" aria-labelledby="h-pred">'
                  f'<h2 id="h-pred">Predictions, written before the run</h2>'
                  f'<p class="small">Hypotheses as stated in {src}; each is reported either way.</p>'
                  f'<ul class="plain hyp">{"".join(f"<li>{inline_md(h)}</li>" for h in hyps)}</ul>'
                  f'<p><strong>Expected result.</strong> {inline_md(expected_result(spec))}</p>'
                  f'<figure>{svg}<figcaption>Figure 1, empty. {e(cfg["figure_caption_preview"])}</figcaption></figure>'
                  f'<p class="small">{e(cfg["preview_note"])}</p></section>')

    sys_html = "".join(f"<li><strong>{s}</strong> {inline_md(t)}</li>" for s, t in systems)
    m = re.search(r"Ground truth comes from [^.]+\. No LLM grades any output\.", spec)
    if not m:
        raise SystemExit("SPEC.md sec. 1: ground-truth sentence not found")
    tested = (f'<section class="s" id="how" aria-labelledby="h-how"><h2 id="h-how">How it is tested</h2>'
              f'<p>{inline_md(m.group(0))} '
              f'<a href="{e(repo.blob("reports/oracle_rules.md"))}">The table, with guideline quotes</a>.</p>'
              f'<details class="all fold"><summary>The edits: three must change the answer, four must not'
              f'</summary>{conditions_html(conds)}</details>'
              f'<details class="all fold"><summary>The five systems</summary><ul class="plain">{sys_html}</ul>'
              f'</details></section>')

    log_sec = (f'<section class="s" id="log" aria-labelledby="h-log"><h2 id="h-log">Log</h2>'
               f'{log_html(log, events, repo, cfg)}</section>')
    lim = (f'<section class="s" id="limits" aria-labelledby="h-lim"><h2 id="h-lim">Limits</h2>'
           f'<ul class="plain">{"".join(f"<li>{inline_md(x)}</li>" for x in limits)}</ul>'
           f'<p class="small"><a href="{e(repo.blob("SPEC.md", limits_anchor))}">All limits (SPEC sec. 11)</a></p></section>')
    cmds = [t for t in cfg["make_targets"] if t in targets]
    cmd_html = (f'<pre><code>{e(chr(10).join(f"make {t}" for t in cmds))}</code></pre>' if cmds else
                '<p class="small">Commands: pending.</p>')
    repro = (f'<section class="s" id="files" aria-labelledby="h-files"><h2 id="h-files">Reproduce and files</h2>'
             f'{cmd_html}{files_html(cfg, repo)}</section>')
    refs_html = "".join(f"<li>{autolink_refs(inline_md(r))}</li>" for r in refs)
    prior_sec = (f'<section class="s" id="prior" aria-labelledby="h-prior"><h2 id="h-prior">Prior work</h2>'
                 f'<p>{autolink_refs(inline_md(prior))}</p><ul class="plain refs">{refs_html}</ul>'
                 f'<p class="small"><a href="{e(repo.blob("SPEC.md", prior_anchor))}">What this adds (SPEC sec. 2)</a></p></section>')
    author = (f'<section class="s" id="author" aria-labelledby="h-author"><h2 id="h-author">Author</h2>'
              f'{user_block(user, "author")}</section>')

    order = ([found, result, tested, log_sec, lim, repro, prior_sec, author] if not results else
             [result, tested, found, log_sec, lim, repro, prior_sec, author])

    css = (SITE / "style.css").read_text()
    js = (SITE / "card.js").read_text()
    og_img = (f'<meta property="og:image" content="{e(cfg["pages_url"])}og.png">'
              if (out_dir / "og.png").exists() or (SITE / "figures" / "og.png").exists() else "")
    state = "results" if results else "preview"
    html = f"""<!doctype html>
<html lang="en" data-state="{state}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>gapcheck: {e(cfg["title"])}</title>
<meta name="description" content="{e(subtitle)}">
<meta property="og:type" content="website">
<meta property="og:title" content="{e(cfg["title"])}">
<meta property="og:description" content="{e(subtitle)}">
<meta property="og:url" content="{e(cfg["pages_url"])}">
{og_img}
<meta name="twitter:card" content="summary_large_image">
<style>
{css}</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap"><a class="brand" href="{e(cfg["pages_url"])}">gapcheck</a><nav aria-label="Links">{" · ".join(nav)}</nav></div></header>
<main id="main" class="wrap">
{hook}
{chr(10).join(order)}
</main>
<footer><div class="wrap">Synthetic patients (Synthea). Ground truth is the guideline as encoded here, not clinical judgment. Research prototype, not medical advice. This page is generated from the repository by <code>scripts/site_build.py</code>.</div></footer>
<script>
{js}</script>
</body>
</html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / "index.html"
    page.write_text(html)
    for name in ("og.png", "card.gif", "figure1.svg", "figure1.png"):
        src = SITE / "figures" / name
        if src.exists():
            shutil.copyfile(src, out_dir / name)
    return page


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="pages")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--results", help="score file to use instead of site/data/score.json "
                    "(for trying the results layout on a mock run; not for publishing)")
    a = ap.parse_args()
    out = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    page = build(out, Path(a.results).resolve() if a.results else None)
    size = page.stat().st_size
    rel = page.relative_to(ROOT) if page.is_relative_to(ROOT) else page
    print(f"wrote {rel} ({size / 1024:.0f} KB)")
    if size > 300 * 1024:
        problem(f"page is {size / 1024:.0f} KB; the budget is 300 KB")
    for p in PROBLEMS:
        print("  note:", p, file=sys.stderr)
    if a.strict and PROBLEMS:
        raise SystemExit(f"--strict: {len(PROBLEMS)} problem(s)")


if __name__ == "__main__":
    main()
