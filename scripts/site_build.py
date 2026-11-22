"""Build the project page (pages/index.html) from repository files.

Usage:
  python scripts/site_build.py [--out pages] [--strict] [--public] [--results FILE]

Inputs (all in the repository; nothing is fetched, at build time or by the page):
  site/config.json      page text that is not the author's prose: title, subtitle,
                        section text, the test steps, links, limits to show
  site/user_text.md     sentences the author writes (placeholders until then)
  site/log.json         the timeline (at most 6 rows shown)
  site/events.json      public upstream events with server timestamps and state
                        (scripts/site_events.py --refresh)
  site/upstream.json    the upstream findings; an item whose pull request is not
                        posted yet is not shown
  site/card.json        the alert card (scripts/site_card.py)
  site/score.json       score file of the pre-registered test run (scripts/score_runs.py;
                        aggregate numbers only). If it is absent the page is in its
                        before-results state; the section order is the same.
  SPEC.md, PROTOCOL.md, RUNS.md, Makefile, git history

Output: one self-contained HTML file (CSS and JS inline, no external requests)
plus the images it or the README refer to. No build timestamp, so the same
inputs give the same bytes.

--strict (use before publishing) stops if an author placeholder is left, a
timeline row does not resolve, or a listed file is missing from the repository.

--public leaves out the author's sections that are not written yet (a public page
before the author's text exists); with --strict every other check still applies.
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
HIDDEN: list[str] = []
PUBLIC = False  # --public: an unwritten author section is left out instead of shown as a placeholder
MAX_TIMELINE = 6


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
    t = re.sub(r"\[([^\]]+)\]\(((?:https?://|#)[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def autolink_refs(html: str) -> str:
    html = re.sub(r"arXiv[ :](\d{4}\.\d{4,5})", r'<a href="https://arxiv.org/abs/\1">arXiv \1</a>', html)
    html = re.sub(r"doi:(10\.\d{4,9}/[^\s,;)]+[^\s,;).])", r'<a href="https://doi.org/\1">doi:\1</a>', html)
    return html


def slug(heading: str) -> str:
    """GitHub's heading anchor."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def nice_date(d: date, year: bool = False) -> str:
    return f"{d.strftime('%b')} {d.day}" + (f", {d.year}" if year else "")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          check=True).stdout


def read(rel: str) -> str | None:
    p = ROOT / rel
    return p.read_text() if p.exists() else None


def pct1(x: float | None) -> str:
    if x is None:
        return "n/a"
    s = f"{100 * x:.1f}"
    return "0.0" if s in ("-0.0", "0.0") else s.replace("-", "−")


def signed(x: float) -> str:
    s = f"{100 * x:+.1f}"
    return s.replace("-", "−")


def no_break_hyphens(text: str) -> str:
    """A hyphen between letters becomes a non-breaking hyphen, so a heading never wraps
    inside "re-checks" or "care-gap"."""
    return re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "\u2011", text)


def join_and(xs: list[str]) -> str:
    xs = list(xs)
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]


NUM_WORD = {2: "Both", 3: "All three", 4: "All four", 5: "All five"}


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
            problem(f"timeline: commit subject {prefix!r} matched {len(hits)} commits")
            return hits[0] if hits else None
        return hits[0]

    def by_sha(self, sha: str) -> tuple[str, datetime] | None:
        hits = [(s, w) for s, w, _ in self.log if s.startswith(sha)]
        return hits[0] if len(hits) == 1 else None


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


def planned_cases(spec: str) -> int:
    m = re.search(r"about (\d[\d,]*) test cases", spec)
    if not m:
        raise SystemExit("SPEC.md: no 'about N test cases'")
    return int(m.group(1).replace(",", ""))


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


def user_filled(sections: dict[str, str], key: str) -> bool:
    body = sections.get(key, "")
    return bool(body) and not re.fullmatch(r"<!--\s*USER:.*?-->", body, re.S)


def user_block(sections: dict[str, str], key: str, cls: str = "") -> str:
    body = sections.get(key, "")
    hint = re.fullmatch(r"<!--\s*USER:\s*(.*?)\s*-->", body, re.S)
    if hint or not body:
        h = " ".join((hint.group(1) if hint else key).split())
        if PUBLIC:
            HIDDEN.append(key)
            return ""
        problem(f"author placeholder left: site/user_text.md ## {key}")
        return f'<!-- USER: {e(h)} --><div class="user" data-slot="{e(key)}">{e(h)}</div>'
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    c = f' class="{cls}"' if cls else ""
    return "".join(f"<p{c}>{inline_md(' '.join(p.split()))}</p>" for p in paras)


# ---------------------------------------------------------------- icons
ICONS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
<symbol id="i-ok" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></symbol>
<symbol id="i-no" viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></symbol>
<symbol id="i-more" viewBox="0 0 16 16"><path d="M6 3.5l4.5 4.5L6 12.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></symbol>
</svg>"""


def icon(name: str, cls: str = "") -> str:
    return f'<svg class="i {cls}" aria-hidden="true" focusable="false"><use href="#i-{name}"/></svg>'


# ---------------------------------------------------------------- card
PILL = {"GAP": "Statin gap", "CANNOT_DETERMINE": "No call", "DOCUMENTED_REASON": "Reason documented",
        "NOT_INDICATED": "Not indicated", "ON_THERAPY": "On a statin", "DISCUSS": "Discuss"}
STATUS_WORDS = {"GAP": "STATIN GAP", "CANNOT_DETERMINE": "NO CALL", "DOCUMENTED_REASON": "REASON DOCUMENTED",
                "NOT_INDICATED": "NOT INDICATED", "ON_THERAPY": "ON A STATIN", "DISCUSS": "DISCUSS"}
SEX = {"M": "Man", "F": "Woman"}
WHERE = {"cpu": "a CPU workstation", "gpu": "one rented GPU"}
PROMPT_NAME = {"task_agent": "agent task", "task_l1": "single-call task", "system": "system",
               "repair": "repair", "reask": "re-ask", "limit": "round-limit"}


def risk_band(r: float) -> str:
    if r >= 0.10:
        return "at or above 10%"
    if r >= 0.05:
        return "between 5% and 10%"
    if r >= 0.03:
        return "between 3% and 5%"
    return "below 3%"


def row_desc(ri: dict | None) -> str:
    if not ri:
        return ""
    return f"row {ri['row']}" + (f", class {ri['class']}" if ri.get("class") else "")


def summary_line(a: dict) -> str:
    st, ri = a["status"], a.get("row_info") or {}
    row = a.get("row")
    risk = f"{100 * a['risk_10y']:.1f}%" if a.get("risk_10y") is not None else None
    if st == "GAP":
        why = {"1": "Clinical ASCVD", "2": "LDL-C 190 mg/dL or higher", "3": "Diabetes at 40 to 75",
               "3b": "CKD stage 3 or 4 at 40 to 75"}.get(row)
        if why is None and risk:
            why = f"10-year risk {risk}"
        return f"{why or ri.get('group', 'Gap')}, no statin on record."
    if st == "CANNOT_DETERMINE":
        miss = [m["lower"] for m in a["missing"]]
        return ("Cannot determine: " + join_and(miss) + " not on record.") if miss else "Cannot determine."
    if st == "DISCUSS":
        return (f"10-year risk {risk}: discuss a statin." if risk and row in ("10", "11a")
                else f"{ri.get('rule') or ri.get('group', '')}: discuss a statin.")
    if st == "NOT_INDICATED":
        return f"No statin indicated ({ri.get('rule') or ri.get('group', '')})."
    if st == "DOCUMENTED_REASON":
        return "Would be a gap; a reason not to prescribe is documented."
    return "On a statin."


def low(label: str) -> str:
    """A label inside a sentence: 'Lipid panel' -> 'lipid panel'; 'eGFR', 'CKD stage 3-4' and
    'Systolic BP' keep their abbreviations."""
    words = label.split(" ")
    if words and words[0][:1].isupper() and not words[0].isupper():
        words[0] = words[0][0].lower() + words[0][1:]
    return " ".join(words)


def failures_text(fails: list[dict]) -> str:
    groups: dict[tuple, list] = {}
    for f in fails:
        groups.setdefault((f["check"], f["kind"]), []).append(f)
    out = []
    for (chk, kind), xs in groups.items():
        names = [low(x["label"] or "") for x in xs]
        n = len(xs)
        if chk == 4 and kind == "missing":
            tail = f"{NUM_WORD.get(n, 'All')} are on record." if n > 1 else "It is on record."
            out.append(f"It listed {join_and(names)} as missing. {tail}")
        elif chk == 4:
            out.append(f"It said {join_and(names)} {'are' if n > 1 else 'is'} not on record. "
                       f"{'They are' if n > 1 else 'It is'}.")
        elif chk == 1:
            out.append(f"A record row it cited for {join_and(names)} does not hold that value.")
        elif chk == 2:
            out.append("It left out an input that its own decision row needs.")
        elif chk == 3:
            out.append("A risk it reported could not be reproduced from the inputs it cited.")
        else:
            out.append(f"Check {chk} failed.")
    return " ".join(out)


def checked_band(t: dict) -> str:
    a, rounds = t["answer"], t["verifier"]
    lines = []
    n_risk = sum(1 for c in a["claims"] if c["support"] == "COMPUTED" and c["name"] == "risk_10y")
    multi = len(rounds) > 1
    for r in rounds:
        if not r["ok"]:
            lines.append(f'<li class="chk rej">{icon("no")}<span><strong>Draft {r["round"]} rejected.</strong> '
                         f'{e(failures_text(r["failures"]))}</span></li>')
        else:
            head = f"Draft {r['round']}: " if multi else ""
            risk = (" Both risk values recomputed." if n_risk == 2 else
                    " The risk was recomputed." if n_risk == 1 else "")
            lines.append(f'<li class="chk ok">{icon("ok")}<span><strong>{e(head)}{r["n_ok"]} of '
                         f'{r["n_claims"]} claims match the record.</strong>{e(risk)}</span></li>')
    if a.get("forced_fallback"):
        lines.append(f'<li class="chk rej">{icon("no")}<span><strong>No alert.</strong> After a failed '
                     f'repair the answer falls back to cannot determine.</span></li>')
    if not rounds:
        lines.append('<li class="chk">Not checked: the agent gave no valid first answer.</li>')
    return (f'<div class="checked"><p class="band">Checked by code</p>'
            f'<ul class="chks">{"".join(lines)}</ul></div>')


def ev_row(ln: dict, risks: list[dict], spec_p1: str, p1_egfr: bool = False) -> str:
    val = ""
    if ln.get("value"):
        val = f'{e(ln.get("sep", " "))}<b>{e(ln["value"])}</b>'
    extra = f'<span class="ev-extra">{e(ln["extra"])}</span>' if ln.get("extra") else ""
    when = ln.get("when") or ""
    mark = (f'{icon("ok", "okc")}<span class="sr">verified</span>' if ln.get("verified") else
            f'{icon("no", "noc")}<span class="sr">failed a check</span>' if ln.get("verified") is False else "")
    det = []
    rows = ln.get("claim_rows") or ln.get("rows") or []
    if rows:
        rows = sorted(rows, key=lambda r: (r.get("date") or "", r["id"]))
        lis = "".join(
            f'<li><code>{e(r["id"])}</code> {e(r["description"] or "")}'
            f'{(" · " + e(r["value"])) if r.get("value") else ""}{(" · " + e(r["date"])) if r.get("date") else ""}'
            f'{" · added by patch P1" if r.get("patch") == "P1" else ""}</li>'
            for r in rows[:4])
        more = f'<li>+ {len(rows) - 4} more rows</li>' if len(rows) > 4 else ""
        det.append(f'<p>Record row{"s" if len(rows) > 1 else ""} cited:</p><ul class="rows">{lis}{more}</ul>')
    if ln["support"] == "COMPUTED":
        if len(risks) == 2 and ln["name"] == "risk_10y":
            v = sorted(risks, key=lambda r: r["smoker"] or 0)
            det.append(f'<p>PREVENT, computed twice: {pct1(v[0]["value"])}% as a non-smoker, '
                       f'{pct1(v[1]["value"])}% as a smoker.</p>')
        if ln.get("inputs"):
            det.append(f'<p>Inputs: {e(ln["inputs"])}.</p>')
            if p1_egfr and "eGFR" in ln["inputs"]:
                det.append(f'<p class="flag">The eGFR was added by <a href="{e(spec_p1)}">patch P1</a>: '
                           f'the generated record had none. Any value from 60 to 140 gives the same answer.</p>')
    if ln["support"] == "ABSENT" and ln.get("source"):
        det.append(f'<p>Fixed query: <code>{e(ln["source"])}</code>.</p>')
    if ln.get("flag") and ln["flag"]["kind"] == "p1":
        det.append(f'<p class="flag">{e(ln["flag"]["text"])} <a href="{e(spec_p1)}">Patches</a>.</p>')
    det.append(f'<p class="meta">{e(ln["support"])} · verifier: {e(ln.get("action") or "")}</p>')
    return (f'<li><details><summary><span class="ev-name">{e(ln["text"])}{val}{extra}</span>'
            f'<span class="ev-when">{e(when)}</span>{mark}{icon("more", "chev")}</summary>'
            f'<div class="ev-more">{"".join(det)}</div></details></li>')


def missing_block(t: dict) -> str:
    a = t["answer"]
    if not a["missing"]:
        return '<p class="miss">Nothing that changes the answer.</p>'
    out = []
    risks = a.get("risks") or []
    both = len(risks) == 2 and {r["smoker"] for r in risks} == {0, 1}
    for m in a["missing"]:
        if m["name"] == "smoking" and both:
            v = sorted(risks, key=lambda r: r["smoker"])
            same = risk_band(v[0]["value"]) == risk_band(v[1]["value"])
            tail = ""
            if same:
                tail = (f" Both are {risk_band(v[0]['value'])}, so "
                        + ("the gap stands." if a["status"] == "GAP" else "the answer is the same either way."))
            out.append(f'<p class="miss"><strong>Smoking status.</strong> The agent computed the risk both '
                       f'ways: {pct1(v[0]["value"])}% if non-smoker, {pct1(v[1]["value"])}% if smoker.{e(tail)}</p>')
        else:
            out.append(f'<p class="miss"><strong>{e(m["label"])}.</strong> {e(m.get("why") or "")}</p>')
    return "".join(out)


def edit_text(ed: dict) -> str:
    must = {"must_not_change": " The answer must not change.",
            "must_change": " The answer must change."}.get(ed.get("direction"), "")
    if ed["kind"] == "delete":
        notes = f"; mentions removed from {ed['notes_changed']} notes" if ed["notes_changed"] else ""
        return (f"Test edit: every {ed['noun']} was deleted from this record on purpose "
                f"({ed['deleted_rows']} rows{notes}).{must}")
    if ed["kind"] == "plant":
        p = ed["planted"][0]
        return f"Test edit: one sentence was added to a note on purpose: “{p['text']}”{must}"
    if ed["kind"] == "move":
        return f"Test edit: one value was moved across a threshold.{must}"
    return "Unedited record."


def key_line(t: dict) -> str:
    k = t["answer_key"]
    if k["status"] == "CANNOT_DETERMINE":
        desc = "cannot determine, " + join_and([m["lower"] for m in k["missing"]]) + " missing"
    else:
        desc = row_desc(k["row_info"])
    if k["same"]:
        return f'<p class="key">Same as the code answer key ({e(desc)}).</p>'
    return (f'<p class="key warn"><strong>Differs from the code answer key</strong>: '
            f'{e(STATUS_WORDS[k["status"]])} ({e(desc)}).</p>')


def prov_text(t: dict) -> str:
    r = t["run"]
    kind = "Test record, one run" if t.get("record") == "test" else "Development record, not a test result"
    d = date.fromisoformat(r["date"]) if r.get("date") else None
    bits = [f"{r['model_display']} on {WHERE[r['where']]}" + (f", {nice_date(d)}" if d else "")]
    if r.get("prompts_changed_since"):
        names = [PROMPT_NAME.get(x, x) for x in r["prompts_changed_since"]]
        bits.append(f"with an earlier version of the {join_and(names)} prompt")
    if r.get("dirty"):
        bits.append("from code that was not yet committed")
    return f"{kind}. " + ", ".join(bits) + ". Example chosen by the author."


def who(p: dict) -> str:
    return f'{SEX.get(p["sex"], p["sex"])}, {p["age"]} · synthetic'


def claims_list(t: dict, spec_p1: str) -> str:
    a = t["answer"]
    lis = []
    for c in a["claims"]:
        mark = (f'{icon("ok", "okc")}<span class="sr">verified</span>' if c.get("verified") else
                f'{icon("no", "noc")}<span class="sr">failed a check</span>' if c.get("verified") is False else "")
        val = f"{e(c.get('sep', ' '))}{e(c['value'])}" if c.get("value") else ""
        flag = (f' <span class="flag">(added by <a href="{e(spec_p1)}">patch P1</a>; any value from '
                f'60 to 140 gives the same answer)</span>' if c.get("flag") else "")
        lis.append(f'<li>{mark}<span>{e(c["text"])}{val}{flag} '
                   f'<span class="tag">{e(c["support"])}</span></span></li>')
    return (f'<details class="sub"><summary>All {len(a["claims"])} claims in the final answer</summary>'
            f'<ul class="claims">{"".join(lis)}</ul></details>')


def panel_html(t: dict, spec_p1: str) -> str:
    a = t["answer"]
    warn = " warn" if t.get("label_warn") else ""
    pill = f'<span class="pill">{e(PILL[a["status"]])}</span>'
    head = (f'<div class="alert-head">{pill}<span class="who">{e(who(t["patient"]))}</span></div>'
            f'<p class="summary{warn}">{e(summary_line(a))}</p>')
    p1 = any(c.get("flag") for c in a["claims"] if c["name"] == "egfr")
    ev = "".join(ev_row(ln, a.get("risks") or [], spec_p1, p1) for ln in a["lines"])
    matters = f'<p class="sec">What matters</p><ul class="ev">{ev}</ul>' if ev else ""
    base = ""
    if t.get("base"):
        b = t["base"]
        risk = f", risk {pct1(b['risk_10y'])}%" if b.get("risk_10y") is not None else ""
        base = (f'<p class="before">Before the edit, on the unedited record, the same agent said: '
                f'<strong>{e(STATUS_WORDS[b["status"]])}</strong>{e(risk)}.</p>')
    note = ""
    if a.get("note_for_clinician"):
        note = (f'<details class="sub"><summary>Agent&rsquo;s note to the clinician</summary>'
                f'<blockquote>{e(a["note_for_clinician"])}</blockquote></details>')
    return (head + checked_band(t) + matters
            + f'<p class="sec">What&rsquo;s missing</p>{missing_block(t)}{base}'
            + f'<p class="edit">{e(edit_text(t["edit"]))}</p>'
            + note + claims_list(t, spec_p1) + key_line(t)
            + f'<p class="prov">{e(prov_text(t))}</p>')


def card_html(card: dict, spec_p1: str) -> str:
    if card.get("schema") != 2:
        raise SystemExit("site/card.json is not schema 2: run scripts/site_card.py")
    tabs = card["tabs"]
    tab_html = "".join(
        f'<button type="button" role="tab" class="tab{" warn" if t.get("label_warn") else ""}" '
        f'id="tab-{e(t["id"])}" aria-controls="panel-{e(t["id"])}" '
        f'aria-selected="{"true" if n == 0 else "false"}" tabindex="{0 if n == 0 else -1}">{e(t["label"])}</button>'
        for n, t in enumerate(tabs))
    panels = "".join(
        f'<section class="panel" id="panel-{e(t["id"])}" role="tabpanel" aria-labelledby="tab-{e(t["id"])}">'
        f'<h3 class="panel-title">{n + 1}. {e(t["label"])}</h3>{panel_html(t, spec_p1)}</section>'
        for n, t in enumerate(tabs))
    return (f'<div class="card" id="card" aria-label="Example care-gap alerts from development runs">'
            f'<div class="tabs" role="tablist" aria-label="Example alerts">{tab_html}</div>{panels}</div>')


def failure_html(t: dict, spec_p1: str, tabs: list[dict] = ()) -> str:
    """A folded 'where it failed' entry built from a real trace (site/card.json failures)."""
    a, k = t["answer"], t["answer_key"]
    ri = a.get("row_info") or {}
    age = t["patient"]["age"]
    rule = ri.get("rule") or ri.get("group") or ""
    m = re.search(r"[Aa]ge[s]? (\d+)\s*[–-]\s*(\d+)", rule)
    off = m and not int(m.group(1)) <= age <= int(m.group(2))
    s1 = (f"The agent answered {STATUS_WORDS[a['status']]} by row {a['row']} ({rule})"
          + (f", but the patient is {age}" if off else "")
          + (", and it never called the risk calculator." if not a.get("called_prevent") else "."))
    v = t["verifier"]
    s2 = ""
    if v and all(r["ok"] for r in v):
        last = v[-1]
        s2 = (f" Every claim it made was on record, so all {last['n_claims']} passed the verifier: "
              f"the verifier checks facts and arithmetic, not which row applies.")
    key = "cannot determine" if k["status"] == "CANNOT_DETERMINE" else row_desc(k["row_info"])
    s3 = f" The code answer key says {STATUS_WORDS[k['status']]} ({key})."
    s4 = ""
    if t.get("after"):
        af = t["after"]
        later = ("with a revised agent task prompt" if
                 (af["run"].get("prompts_sha256") or {}).get("task_agent") !=
                 (t["run"].get("prompts_sha256") or {}).get("task_agent") else "")
        s4 = (f" A later run {later} answered {STATUS_WORDS[af['status']]}"
              + (f" by row {af['row']}" if af.get("row") else "")
              + (", the same as the answer key" if af["same_as_key"] else ""))
        s4 = s4.replace("run  answered", "run answered")
        tab = next((x for x in tabs if x["case_id"] == t["case_id"]
                    and x["run"]["run_id"] == af["run"]["run_id"]), None)
        s4 += f" (the \u201c{tab['label']}\u201d example above)." if tab else "."
    return (f'<p class="small">{e(who(t["patient"]))}. {e(edit_text(t["edit"]))}</p>'
            f'<p>{e(s1 + s2 + s3 + s4)}</p>'
            f'<p class="prov">{e(prov_text(t))}</p>')


# ---------------------------------------------------------------- page parts
def steps_html(cfg: dict, repo: Repo, fields: dict, runs: dict) -> tuple[str, dict]:
    """The test's steps with their dates from RUNS.md; a step not done is a hollow dot."""
    done = {}
    items = []
    for st in cfg["test_steps"]:
        when, url = None, None
        if "run_row" in st:
            row = runs.get(st["run_row"])
            fin = row[5] if row and len(row) > 5 else None
            if not is_pending(fin):
                when = parse_ts(fin) if "T" in fin else datetime.fromisoformat(fin[:10]).replace(tzinfo=timezone.utc)
                url = repo.blob("RUNS.md", st.get("anchor", ""))
        elif "commit_field" in st:
            v = field(fields, st["done_field"])
            c = field(fields, st["commit_field"])
            if not is_pending(v):
                hit = repo.by_sha(c.strip("` ")) if c and not is_pending(c) else None
                when = hit[1] if hit else None
                url = repo.blob("RUNS.md", st.get("anchor", ""))
                if when is None:
                    when = "done"
        else:
            v = field(fields, st["date_field"])
            if not is_pending(v):
                when = parse_ts(v.strip("` "))
                u = field(fields, st.get("url_field", "")) if st.get("url_field") else None
                url = u.strip("<>` ") if u and not is_pending(u) else repo.blob("RUNS.md", st.get("anchor", ""))
        done[st["key"]] = when
        lab = e(st["label"])
        if when:
            d = "" if when == "done" else f' <time datetime="{when.date().isoformat()}">{nice_date(when.date())}</time>'
            items.append(f'<li class="done"><span class="dot" aria-hidden="true"></span>'
                         f'<a href="{e(url)}">{lab}{d}</a><span class="sr"> (done)</span></li>')
        else:
            items.append(f'<li><span class="dot" aria-hidden="true"></span>{lab}'
                         f'<span class="sr"> (not yet)</span></li>')
    return f'<ol class="steps" aria-label="Test steps">{"".join(items)}</ol>', done


def timeline_html(log: dict, events: dict, repo: Repo, fields: dict, runs: dict) -> list[tuple]:
    """Rows (date, html); a row whose date comes from RUNS.md is left out until it is filled."""
    rows = []
    for n, r in enumerate(log["rows"]):
        when, links = None, []
        if "runs_field" in r:
            v = field(fields, r["runs_field"])
            if is_pending(v):
                continue
            when = parse_ts(v.strip("` "))
        if "run_row" in r:
            row = runs.get(r["run_row"])
            fin = row[5] if row and len(row) > 5 else None
            if is_pending(fin):
                continue
            when = parse_ts(fin) if "T" in fin else datetime.fromisoformat(fin[:10]).replace(tzinfo=timezone.utc)
        for ln in r.get("links", []):
            if "commit" in ln:
                hit = repo.find(ln["commit"])
                if hit:
                    links.append(f'<a href="{e(repo.commit_url(hit[0]))}">{e(ln["label"])}</a>')
                    when = when or hit[1]
            elif "first_commit" in ln:
                sha, w = repo.first_commit()
                links.append(f'<a href="{e(repo.commit_url(sha))}">{e(ln["label"])}</a>')
                when = when or w
            elif "event" in ln:
                ev = events.get(ln["event"])
                if not ev:
                    problem(f"timeline: unknown event {ln['event']}")
                    continue
                links.append(f'<a href="{e(ev["url"])}">{e(ln["label"])}</a>')
                when = when or parse_ts(ev["created_at"])
            elif "path" in ln:
                if not tracked(ln["path"]):
                    problem(f"timeline: {ln['path']} is not tracked")
                links.append(f'<a href="{e(repo.blob(ln["path"]))}">{e(ln["label"])}</a>')
            elif "url" in ln:
                links.append(f'<a href="{e(ln["url"])}">{e(ln["label"])}</a>')
        if r.get("date"):
            typed = date.fromisoformat(r["date"])
            if when and when.date() != typed:
                problem(f"timeline row {n + 1}: typed date {typed} differs from its record {when.date()}")
            when = when or datetime(typed.year, typed.month, typed.day, tzinfo=timezone.utc)
        if when is None:
            problem(f"timeline row {n + 1}: no date")
            continue
        ls = (" " + " · ".join(links)) if links else ""
        rows.append((when, n, f"{inline_md(r['text'])}{ls}"))
    rows.sort(key=lambda x: (x[0], x[1]))
    if len(rows) > MAX_TIMELINE:
        problem(f"timeline has {len(rows)} rows; the page shows at most {MAX_TIMELINE}")
    return rows


def upstream_html(items: list[dict], events: dict) -> str:
    out = []
    for it in items:
        ev = events.get(it.get("event") or "")
        if not ev:
            continue  # not posted yet
        state = "merged" if ev.get("merged_at") else ev.get("state", "")
        chk = f" (checked {nice_date(date.fromisoformat(ev['checked_at']))})" if ev.get("checked_at") else ""
        meta = [f'<a href="{e(ev["url"])}">PR #{ev["number"]}</a> · {e(state)}{e(chk)}']
        tail = ""
        if it.get("reply"):
            rev = events.get(it["reply"]["event"])
            if not rev:
                problem(f"upstream: unknown reply event {it['reply']['event']}")
            else:
                tail = f' {e(it["reply"]["text"])} <a href="{e(rev["url"])}">(his reply)</a>'
        detail = f'<p class="up-d">{inline_md(it["detail"])}</p>' if it.get("detail") else ""
        note = f'<p class="up-d small">{inline_md(it["note"])}</p>' if it.get("note") else ""
        out.append(f'<li><p class="up-f">{inline_md(it["finding"])}</p>{detail}'
                   f'<p class="up-m">{"".join(meta)}.{tail}</p>{note}</li>')
    return f'<ul class="up">{"".join(out)}</ul>'


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
            + "".join(out) + '<details class="sub"><summary>What each edit does</summary>'
            f'<ul class="plain">{det}</ul></details>')


def files_html(cfg: dict, repo: Repo) -> str:
    lis = []
    for f in cfg["files"]:
        if tracked(f["path"]):
            lis.append(f'<li><a href="{e(repo.blob(f["path"]))}">{e(f["label"])}</a></li>')
        else:
            if not f.get("pending_text"):
                problem(f"files: {f['path']} is not in the repository yet")
            lis.append(f'<li>{e(f["label"])} <span class="muted">({e(f.get("pending_text", "later"))})</span></li>')
    return f'<ul class="plain">{"".join(lis)}</ul>'


SYS_LABEL = {"R0": "Rule on structured data", "R1": "Keyword rule", "L1": "Single model call",
             "A1": "Agent before the verifier", "A2": "Agent with a code verifier"}
# PROTOCOL sec. 10 "Display": one example patient's edits, in this order
RESULT_ROWS = (("C0", "Record as is"), ("C3a", "One sentence added to a note"),
               ("C1", "Lipid panel deleted (or another deciding input)"))
BY_CONSTRUCTION = "answer key, correct by construction (not scored)"


def k_of_n(v: dict | None, with_ci: bool = True) -> str:
    """'k of n (p%, 95% CI lo to hi)': denominator first, then the rate (PROTOCOL sec. 10)."""
    if not v or v.get("n") is None:
        return "n/a"
    rate = v.get("rate", v.get("est"))
    ci = v.get("ci") or [None, None]
    ci_t = f", 95% CI {pct1(ci[0])} to {pct1(ci[1])}" if with_ci and ci[0] is not None else ""
    return f'{v["k"]} of {v["n"]}' + (f" ({pct1(rate)}%{ci_t})" if rate is not None else "")


def diff_text(d: dict | None) -> str:
    if not d or d.get("est") is None:
        return "n/a"
    ci = d.get("ci") or [None, None]
    lo = signed(ci[0]) if ci[0] is not None else "n/a"
    hi = signed(ci[1]) if ci[1] is not None else "n/a"
    return f"{signed(d['est'])} points (95% CI {lo} to {hi})"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def count_tile(big: dict, label: str, others: list[tuple[str, dict | None]] = (),
               complement: bool = False, extra: str = "") -> str:
    k, n = big["k"], big["n"]
    rate, ci = big.get("rate"), big.get("ci") or [None, None]
    if complement:
        k = n - k
        rate = None if rate is None else 1 - rate
        ci = [None if ci[1] is None else 1 - ci[1], None if ci[0] is None else 1 - ci[0]]
    ci_t = f", 95% CI {pct1(ci[0])} to {pct1(ci[1])}" if ci[0] is not None else ""
    rate_t = f"({pct1(rate)}%{ci_t})" if rate is not None else ""
    side = ""
    for lab, o in others:
        if o:
            ok = o["n"] - o["k"] if complement else o["k"]
            side += f'<span class="vs">{e(lab)}: {ok} of {o["n"]}</span>'
    if extra:
        side += f'<span class="vs">{e(extra)}</span>'
    return (f'<li><span class="big">{k} <span class="of">of</span> {n}</span>'
            f'<span class="txt">{e(label)} <span class="rate">{e(rate_t)}</span>{side}</span></li>')


def forced_on(res: dict, conds) -> int | None:
    ff = ((res.get("secondary") or {}).get("A2") or {}).get("forced_fallbacks")
    if ff is None:
        return None
    return sum(int(ff.get(c, 0)) for c in conds)


def rows_table(res: dict, conds: dict) -> str:
    """The three rows of PROTOCOL sec. 10: rules (labelled as the answer key where they are
    correct by construction), A1 and A2 with A2's forced fallbacks on that condition."""
    acc = (res.get("accuracy_main") or {}).get("systems") or {}

    def cell(s, c):
        return k_of_n(((acc.get(s) or {}).get("by_condition") or {}).get(c), with_ci=False)

    body = []
    for c, label in RESULT_ROWS:
        if c not in conds:
            problem(f"results: condition {c} not in SPEC")
            continue
        if c == "C3a":
            rules = (f'<span class="rl">{e(SYS_LABEL["R0"])}: {e(cell("R0", c))}</span>'
                     f'<span class="rl">{e(SYS_LABEL["R1"])}: {e(cell("R1", c))}</span>')
        else:
            rules = f'<span class="rl muted">{e(BY_CONSTRUCTION)}</span>'
        f = forced_on(res, (c,))
        a2 = (f'<strong>{e(cell("A2", c))}</strong>'
              + ("" if f is None else f'<span class="small ff">{plural(f, "forced fallback")}</span>'))
        if not acc.get("A2"):
            problem("results: no per-condition accuracy for A2")
        correct = "the answer-key label" if c == "C0" else inline_md(conds[c]["correct"])
        body.append(f'<tr><th scope="row">{e(label)}<span class="small">{e(c)} · correct: '
                    f'{correct}</span></th>'
                    f'<td data-l="Rules">{rules}</td>'
                    f'<td data-l="{e(SYS_LABEL["A1"])}">{e(cell("A1", c))}</td>'
                    f'<td data-l="{e(SYS_LABEL["A2"])}">{a2}</td></tr>')
    head = (f'<thead><tr><th scope="col">Edit to the record</th><th scope="col">Rules</th>'
            f'<th scope="col">{e(SYS_LABEL["A1"])} (A1)</th><th scope="col">{e(SYS_LABEL["A2"])} (A2)</th>'
            f'</tr></thead>')
    return f'<table class="rows3">{head}<tbody>{"".join(body)}</tbody></table>'


def weakest_text(res: dict, conds: dict) -> str:
    w = res.get("weakest_A2")
    if not w:
        problem("results: no weakest condition for A2")
        return ""
    c = w["condition"]
    a1 = ((((res.get("accuracy_main") or {}).get("systems") or {}).get("A1") or {})
          .get("by_condition") or {}).get(c)
    f = forced_on(res, (c,))
    name = conds.get(c, {}).get("name", "")
    return (f'<p class="weak"><strong>Weakest condition for the agent with a code verifier:</strong> '
            f'{e(c)} ({e(name)}), {e(k_of_n(w))}'
            + ("" if f is None else f', {plural(f, "forced fallback")}')
            + (f'; before the verifier, {e(k_of_n(a1, with_ci=False))}' if a1 else "") + '.</p>')


def checks_text(res: dict) -> str:
    sec = res.get("secondary") or {}
    parts = [f'{SYS_LABEL[s].lower()} {k_of_n(sec[s]["passes_all_checks"], with_ci=False)}'
             for s in ("L1", "A1", "A2") if (sec.get(s) or {}).get("passes_all_checks")]
    if not parts:
        return ""
    return (f'<p class="small">Final answers whose evidence passes all four checks: '
            f'{e(join_and(parts))}. The verifier\'s answers pass or are withheld by construction; '
            f'passing means the evidence was re-checked, not that the answer is correct.</p>')


def results_block(res: dict, user: dict, fig_svg: str, cfg: dict, fields: dict,
                  run_date: date | None, fails_html: str, conds: dict) -> str:
    head = res.get("headline") or {}
    p = head.get("primary")
    prim = ""
    if p:
        prim = (f'<p class="primary"><strong>Primary outcome (pre-registered):</strong> exact-status accuracy '
                f'averaged over the eight test conditions, agent with a code verifier {pct1(p["A2"]["est"])}% '
                f'against single model call {pct1(p["L1"]["est"])}%, difference {diff_text(p["difference"])}. '
                f'Tools alone (A1 − L1): {diff_text(p.get("A1-L1"))}. '
                f'The verifier and its one repair (A2 − A1): {diff_text(p.get("A2-A1"))}.</p>')
        if p.get("A2-A1") is None:
            problem("results: no A2 − A1 difference in the score file")
    else:
        problem("results: no primary outcome (A2 or L1 missing from the score file)")
    tiles = []
    ch = head.get("changed_when_must_not") or {}
    if "A2" in ch:
        f = forced_on(res, ("C2a", "C2b", "C3b", "C4"))
        tiles.append(count_tile(ch["A2"], cfg["result_tiles"]["kept"],
                                [(SYS_LABEL["A1"], ch.get("A1")), (SYS_LABEL["L1"], ch.get("L1"))],
                                complement=True,
                                extra="" if f is None else f'Forced fallbacks on these edits: {f}'))
    else:
        problem("results: no answer-change counts for A2")
    v = res.get("verifier_A2")
    if v:
        lab = cfg["result_tiles"]["verifier"].format(repaired=v["repaired"], forced=v["forced"])
        tiles.append(count_tile(v["rejected"], lab))
    else:
        problem("results: no verifier counts for A2")
    model = field(fields, "Model repository")
    gpu = field(fields, "GPU, provider")
    bits = ["Test set", "one run"]
    if model and not is_pending(model):
        bits.append(model.split(",")[0].strip("` ").split("@")[0].split("/")[-1]
                    + (f" on {gpu.split(',')[0].strip('` ')}" if gpu and not is_pending(gpu) else ""))
    if run_date:
        bits.append(nice_date(run_date, year=True))
    fig = (f'<details class="sub"><summary>Accuracy by condition (Figure 1)</summary>'
           f'<figure>{fig_svg}<figcaption>Figure 1. Percent correct by condition and system, 95% cluster-'
           f'bootstrap intervals. R0 and R1 contain the answer key, so they are correct by construction on '
           f'C0, C1, C2a, C2b and C5 (open squares).</figcaption></figure></details>')
    return (f'<p>{e(cfg["test_post"].format(n=res["n_cases"], p=res.get("n_patients") or "?"))}</p>'
            + rows_table(res, conds) + weakest_text(res, conds) + prim
            + f'<ul class="tiles">{"".join(tiles)}</ul>' + checks_text(res)
            + user_block(user, "result")
            + f'<p class="small muted">{e(" · ".join(bits))}</p>{fig}{fails_html}')


def load_figure1():
    spec = importlib.util.spec_from_file_location("figure1", ROOT / "scripts" / "figure1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fold(id_: str, title: str, body: str) -> str:
    return (f'<details class="fold" id="{e(id_)}"><summary><span>{e(title)}</span>{icon("more", "chev")}'
            f'</summary><div class="fold-body">{body}</div></details>')


# ---------------------------------------------------------------- page
def build(out_dir: Path, res_p: Path | None = None) -> Path:
    cfg = json.loads((SITE / "config.json").read_text())
    card = json.loads((SITE / "card.json").read_text())
    log = json.loads((SITE / "log.json").read_text())
    events = {x["id"]: x for x in json.loads((SITE / "events.json").read_text())["events"]}
    upstream = json.loads((SITE / "upstream.json").read_text())
    user = user_sections((SITE / "user_text.md").read_text())
    res_p = res_p or SITE / "score.json"
    results = json.loads(res_p.read_text()) if res_p.exists() else None
    spec, protocol, runs_text = read("SPEC.md"), read("PROTOCOL.md"), read("RUNS.md")
    repo = Repo(cfg)
    fields, runs = runs_md(runs_text)
    steps, done = steps_html(cfg, repo, fields, runs)
    released = bool(done.get("protocol"))
    run_end = done.get("run") if isinstance(done.get("run"), datetime) else None
    fig1 = load_figure1()

    sha0, first = repo.first_commit()
    f0 = first.date()
    spec_p1 = repo.blob("SPEC.md", slug(re.search(r"^### (4\.1 [^\n]+)", spec, re.M).group(1)))

    # hero
    if results and run_end:
        end = run_end.date()
        span = (f"{nice_date(f0)}–{end.day}" if end.month == f0.month else
                f"{nice_date(f0)}–{nice_date(end)}")
        eyebrow = cfg["eyebrow_post"].format(span=span, year=end.year)
    else:
        eyebrow = cfg["eyebrow_pre"].format(first=nice_date(f0, year=True))
    nav = [f'<a href="{e(repo.url)}">Code</a>', f'<a href="{e(repo.blob("PROTOCOL.md"))}">Protocol</a>']
    if tracked(cfg["summary_pdf"]):
        nav.append(f'<a href="{e(repo.blob(cfg["summary_pdf"]))}">Summary (PDF)</a>')
    hero = (f'<section class="hero" aria-labelledby="h-title"><div class="hero-text">'
            f'<p class="eyebrow">{" · ".join(f"<span>{e(x)}</span>" for x in eyebrow.split(" · "))}</p>'
            f'<h1 id="h-title">{no_break_hyphens(e(cfg["title"]))}</h1>'
            f'<p class="sub">{e(cfg["subtitle"])}</p>'
            f'<p class="hero-links"><a href="#how">How it works</a> · <a href="#test">The test</a> · '
            f'<a href="#upstream">Fixes sent upstream</a></p></div>'
            f'<div class="hero-card">{card_html(card, spec_p1)}</div></section>')

    # 2. how it works
    how = "".join(f'<li><strong>{e(h)}</strong> {inline_md(t)}</li>' for h, t in cfg["how_it_works"])
    how_sec = (f'<section class="s" id="how" aria-labelledby="h-how"><h2 id="h-how">How it works</h2>'
               f'<ol class="how">{how}</ol><p class="note">{e(cfg["how_it_works_note"])}</p></section>')

    # 3. the test / results
    fails = card.get("failures") or []
    fails_body = "".join(failure_html(t, spec_p1, card["tabs"]) for t in fails)
    if results:
        svg = fig1.render(results, inline=True)
        fail_fold = (f'<details class="sub"><summary>Where it failed: {len(fails)} record{"s" if len(fails) != 1 else ""}</summary>'
                     f'{fails_body}</details>' if fails else "")
        test_body = results_block(results, user, svg, cfg, fields, run_end.date() if run_end else None,
                                  fail_fold, spec_conditions(spec))
    else:
        n = planned_cases(spec)
        test_body = (f'<p class="lead">{e(cfg["test_lead"])}</p>'
                     f'<p>{e(cfg["test_pre"].format(n=f"{round(n, -2):,}"))}</p>{steps}'
                     f'<p class="small muted">{e(cfg["test_steps_note_released" if released else "test_steps_note"])}'
                     f' <a href="{e(repo.blob("RUNS.md"))}">RUNS.md</a></p>')
    test_sec = (f'<section class="s" id="test" aria-labelledby="h-test"><h2 id="h-test">Does it hold up?</h2>'
                f'{test_body}</section>')

    # 4. timeline
    rows = timeline_html(log, events, repo, fields, runs)
    if results and run_end:
        n_days = (run_end.date() - f0).days + 1
        tl_title = f"First commit to tested result: {n_days} days"
    else:
        tl_title = f"Started {nice_date(f0)}"
    tl = "".join(f'<li><time datetime="{w.date().isoformat()}">{nice_date(w.date())}</time>'
                 f'<span>{h}</span></li>' for w, _, h in rows[:MAX_TIMELINE])
    if user_filled(user, "how_fast"):
        fast = user_block(user, "how_fast", "disclose")
        if not re.search(r"\bAI\b", user.get("how_fast", "")):
            problem("how_fast does not mention AI; the AI disclosure sentence is no longer on the page")
    else:
        fast = user_block(user, "how_fast") + f'<p class="disclose">{inline_md(cfg["ai_disclosure"])}</p>'
    tl_sec = (f'<section class="s" id="timeline" aria-labelledby="h-tl"><h2 id="h-tl">{e(tl_title)}</h2>'
              f'<ol class="tl">{tl}</ol>{fast}</section>')

    # 5. upstream
    up_sec = (f'<section class="s" id="upstream" aria-labelledby="h-up"><h2 id="h-up">{e(cfg["upstream_title"])}</h2>'
              f'<p>{e(cfg["upstream_intro"])}</p>{upstream_html(upstream["items"], events)}'
              f'{user_block(user, "upstream")}</section>')

    # 6. deployability
    dep = f'<p>{e(cfg["deploy_text"])}</p>'
    if results and results.get("cost"):
        c = results["cost"]
        dep += (f'<p class="dep">{e(cfg["deploy_post"].format(s=round(c["gpu_seconds_per_case"]), c=f"{c['usd_per_case']:.3f}"))}</p>')
    elif cfg.get("deploy_dev_timing"):
        dep += f'<p class="dep">{e(cfg["deploy_dev_timing"])}</p>'
    dep_sec = (f'<section class="s" id="deploy" aria-labelledby="h-dep"><h2 id="h-dep">Runs where the data lives</h2>'
               f'{dep}</section>')

    # 7. rigor line
    rel_url = field(fields, "GitHub Release URL")
    links = {"protocol": (rel_url.strip("<>` ") if released and rel_url and not is_pending(rel_url)
                          else repo.blob("PROTOCOL.md")),
             "beacon": repo.blob("RUNS.md", "2-beacon-pulse-and-test-seed"),
             "oracle": repo.blob("reports/oracle_rules.md"), "reproduce": repo.blob("docs/REPRODUCE.md")}
    rig = []
    for it in cfg["rigor"]:
        text = it["text_released"] if (released and "text_released" in it) else it["text"]
        rig.append(f'<a href="{e(links[it["link"]])}">{e(text)}</a>')
    rigor = f'<p class="rigor">{" · ".join(rig)}</p>'

    # 8-9
    nb, ab = user_block(user, "next"), user_block(user, "author")
    next_sec = (f'<section class="s s-tight" id="next" aria-labelledby="h-next"><h2 id="h-next">Next</h2>'
                f'{nb}</section>') if nb else ""
    author = (f'<section class="s s-tight" id="author" aria-labelledby="h-author"><h2 id="h-author">Author</h2>'
              f'{ab}</section>') if ab else ""

    # folded
    conds, systems = spec_conditions(spec), spec_systems(spec)
    m = re.search(r"Ground truth comes from [^.]+\. No LLM grades any output\.", spec)
    if not m:
        raise SystemExit("SPEC.md sec. 1: ground-truth sentence not found")
    sys_html = "".join(f"<li><strong>{s}</strong> {inline_md(t)}</li>" for s, t in systems)
    tested = (f'<p>{inline_md(m.group(0))} <a href="{e(repo.blob("reports/oracle_rules.md"))}">'
              f'The decision table, with guideline quotes</a>.</p>{conditions_html(conds)}'
              f'<h3>The five systems</h3><ul class="plain">{sys_html}</ul>')
    targets = make_targets(read("Makefile"))
    cmds = [t for t in cfg["make_targets"] if t in targets]
    cmd_html = (f'<pre><code>{e(chr(10).join(f"make {t}" for t in cmds))}</code></pre>' if cmds else "")
    limits, lim_anchor = spec_limits(spec, cfg["limits_folded"])
    lim = (f'<ul class="plain">{"".join(f"<li>{inline_md(x)}</li>" for x in limits)}</ul>'
           f'<p class="small"><a href="{e(repo.blob("SPEC.md", lim_anchor))}">All limits (SPEC sec. 11)</a></p>')
    refs, prior, prior_anchor = prior_work(spec)
    refs_html = "".join(f"<li>{autolink_refs(inline_md(r))}</li>" for r in refs)
    prior_body = (f'<p>{autolink_refs(inline_md(prior))}</p><ul class="plain refs">{refs_html}</ul>'
                  f'<p class="small"><a href="{e(repo.blob("SPEC.md", prior_anchor))}">What this adds (SPEC sec. 2)</a></p>')
    folds = [fold("more-tested", "How it is tested", tested)]
    if not results and fails:
        folds.append(fold("more-failed", cfg["failed_title_pre"], fails_body))
    folds += [fold("more-files", "Reproduce and files", cmd_html + files_html(cfg, repo)),
              fold("more-limits", "Limits", lim), fold("more-prior", "Prior work", prior_body)]
    more = f'<section class="more" aria-label="More detail">{"".join(folds)}</section>'

    footer = (f'<footer><div class="wrap"><p>{e(cfg["footer"])} '
              f'<a href="{e(repo.blob("SPEC.md", lim_anchor))}">All limits</a>.</p>'
              f'<p class="muted">This page is generated from the repository by <code>scripts/site_build.py</code>; '
              f'it loads nothing from other sites.</p></div></footer>')

    css = (SITE / "style.css").read_text()
    js = (SITE / "card.js").read_text()
    og_img = (f'<meta property="og:image" content="{e(cfg["pages_url"])}og.png">'
              if (out_dir / "og.png").exists() or (SITE / "figures" / "og.png").exists() else "")
    state = "results" if results else "preview"
    desc = cfg["subtitle"]
    html = f"""<!doctype html>
<html lang="en" data-state="{state}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>gapcheck: {e(cfg["title"])}</title>
<meta name="description" content="{e(desc)}">
<meta name="color-scheme" content="light dark">
<meta property="og:type" content="website">
<meta property="og:title" content="{e(cfg["title"])}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(cfg["pages_url"])}">
{og_img}
<meta name="twitter:card" content="summary_large_image">
<style>
{css}</style>
</head>
<body>
{ICONS}
<a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap"><a class="brand" href="{e(cfg["pages_url"])}">gapcheck</a><nav aria-label="Links">{" · ".join(nav)}</nav></div></header>
<main id="main" class="wrap">
{hero}
<div class="body">
{how_sec}
{test_sec}
{tl_sec}
{up_sec}
{dep_sec}
{rigor}
{next_sec}
{author}
{more}
</div>
</main>
{footer}
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
    ap.add_argument("--public", action="store_true",
                    help="leave out author sections not written yet (for a public page before the "
                         "author's text exists); every other --strict check still applies")
    ap.add_argument("--results", help="score file to use instead of site/score.json "
                    "(for trying the results layout on a mock run; not for publishing)")
    a = ap.parse_args()
    global PUBLIC
    PUBLIC = a.public
    out = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
    page = build(out, Path(a.results).resolve() if a.results else None)
    size = page.stat().st_size
    rel = page.relative_to(ROOT) if page.is_relative_to(ROOT) else page
    print(f"wrote {rel} ({size / 1024:.0f} KB)")
    if size > 300 * 1024:
        problem(f"page is {size / 1024:.0f} KB; the budget is 300 KB")
    for p in PROBLEMS:
        print("  note:", p, file=sys.stderr)
    if HIDDEN:
        print("  left out (not written yet):", ", ".join(HIDDEN), file=sys.stderr)
    if a.strict and PROBLEMS:
        raise SystemExit(f"--strict: {len(PROBLEMS)} problem(s)")


if __name__ == "__main__":
    main()
