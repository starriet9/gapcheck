"""Figure 1 (SPEC 9): exact-status accuracy by condition, 95% cluster-bootstrap CIs.

Usage:
  python scripts/figure1.py --score data/runs/<run>/score.json [--out-dir site/figures]
  python scripts/figure1.py --empty [--out-dir site/figures]      # frame only, no data

Writes figure1.svg (light, white background, for README and print), figure1.png
(if ffmpeg with librsvg is on PATH) and figure1_table.csv (every system,
including R0, with k, n, estimate and interval). `render()` is also used by
scripts/site_build.py to draw the same figure inline, styled by the page.

Layout: one block per condition (C0 on top, then the edits that must change
the answer, C1 C3a C5, then the edits that must not, C2a C2b C3b C4), the
condition name on its own line, one sub-row per system. Marks: R1 gray square,
L1 blue circle, A1 orange diamond, A2 aqua triangle (shape and color both carry
identity). R1 cells that are correct by construction (C0, C1, C2a, C2b, C5;
SPEC 6) are open squares. R0 is in the table only (SPEC 9). Numbers come only
from the score file; nothing is typed in.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMS = ("R1", "L1", "A1", "A2")
TABLE_SYSTEMS = ("R0", "R1", "L1", "A1", "A2")
GROUPS = (("", ("C0",)),
          ("Edits that must change the answer", ("C1", "C3a", "C5")),
          ("Edits that must not change it", ("C2a", "C2b", "C3b", "C4")))
BY_CONSTRUCTION = {"R0": {"C0", "C1", "C2a", "C2b", "C5"}, "R1": {"C0", "C1", "C2a", "C2b", "C5"}}
SYSTEM_NAME = {"R0": "R0 rules, structured data", "R1": "R1 rules + keywords",
               "L1": "L1 one model call", "A1": "A1 agent", "A2": "A2 agent + verifier"}
# Light colors (validated: dataviz reference palette slots 1-3, all-pairs PASS;
# aqua is below 3:1 on white, so shapes, legend and the table carry it too).
LIGHT = {"bg": "#ffffff", "ink": "#111111", "ink2": "#4a4a4a", "rule": "#d9d9d6",
         "grid": "#ececea", "R1": "#6b6b6b", "L1": "#2a78d6", "A1": "#eb6834", "A2": "#1baf7a"}

W = 360            # viewBox width (phone width; the page scales it)
LEFT, RIGHT = 14, 16   # text margin; the plot keeps 8 more on each side for marks
PAD = 8
LABEL_H, SUB_H, GROUP_H, GAP = 18, 12, 26, 8


def condition_names(spec: Path = ROOT / "SPEC.md") -> dict[str, str]:
    """Condition names from the SPEC 5.2 table, e.g. 'C1' -> 'deciding fact deleted'."""
    names = {}
    for ln in spec.read_text().splitlines():
        m = re.match(r"\|\s*(C\d[ab]?)\s+([^|]+?)\s*\|", ln)
        if m:
            names[m.group(1)] = m.group(2)
    need = {c for _, cs in GROUPS for c in cs}
    if not need <= names.keys():
        raise SystemExit(f"SPEC 5.2 table: no name for {sorted(need - names.keys())}")
    return names


def x_of(p: float) -> float:
    return LEFT + PAD + p * (W - LEFT - RIGHT - 2 * PAD)


def wrap(text: str, width: int = 50) -> list[str]:
    """At most two lines, broken at the last ', ' or space before `width`."""
    if len(text) <= width:
        return [text]
    cut = text.rfind(", ", 0, width)
    cut = cut + 1 if cut > 0 else text.rfind(" ", 0, width)
    return [text[:cut].strip(), text[cut:].strip()]


def marker(system: str, x: float, y: float, hollow: bool, cls: bool) -> str:
    fill = "none" if hollow else ("var(--f1-%s)" % system if cls else LIGHT[system])
    stroke = "var(--f1-%s)" % system if cls else LIGHT[system]
    sw = 1.6 if hollow else 1.0
    if system in ("R0", "R1"):
        return (f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" rx="1" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    if system == "L1":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.3" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
    if system == "A1":
        return (f'<path d="M{x:.1f} {y - 5:.1f}L{x + 5:.1f} {y:.1f}L{x:.1f} {y + 5:.1f}L{x - 5:.1f} {y:.1f}Z" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    return (f'<path d="M{x:.1f} {y - 5:.1f}L{x + 5:.1f} {y + 4:.1f}L{x - 5:.1f} {y + 4:.1f}Z" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def pct(v: float) -> str:
    return f"{100 * v:.0f}"


def render(score: dict | None, inline: bool = False, systems=SYSTEMS) -> str:
    """The figure as an SVG string. score=None draws the empty frame.

    inline=True: colors are CSS variables (--f1-ink, --f1-L1, ...) set by the
    page for light and dark; no background. inline=False: fixed light colors
    on white, for the standalone file.
    """
    names = condition_names()
    acc = score["accuracy_main"] if score else None
    sub_h = SUB_H if score else 5  # the empty frame keeps the rows, not their height
    if acc:  # legend and rows only for systems the score file has
        systems = tuple(s for s in systems if s in acc["systems"])
    c = (lambda k: f"var(--f1-{k})") if inline else (lambda k: LIGHT[k])
    rows = []
    y = 6
    rows.append(f'<text x="{LEFT}" y="{y + 11}" class="f1t" fill="{c("ink2")}">'
                f'Percent correct (exact status), 95% CI</text>')
    y += 22
    top = y
    marks = []
    blocks = []
    for title, conds in GROUPS:
        if title:
            y += 6
            rows.append(f'<text x="{LEFT}" y="{y + 13}" class="f1g" fill="{c("ink")}" '
                        f'font-weight="600">{escape(title)}</text>')
            y += GROUP_H - 6
        for cond in conds:
            n = acc["n_cases"].get(cond) if acc else None
            lab = f"{cond} {names[cond]}" + (f" · n = {n}" if n else "")
            for part in wrap(lab):
                rows.append(f'<text x="{LEFT}" y="{y + 12}" class="f1l" fill="{c("ink")}">{escape(part)}</text>')
                y += LABEL_H - 3
            y += 3
            blocks.append((y, y + sub_h * len(systems)))
            for s in systems:
                cy = y + sub_h / 2
                v = (acc["systems"].get(s, {}).get("by_condition", {}).get(cond) if acc else None)
                if v and v.get("est") is not None:
                    lo, hi = v["ci"] if v.get("ci") and None not in v["ci"] else (v["est"], v["est"])
                    hollow = cond in BY_CONSTRUCTION.get(s, set())
                    k = round(v["est"] * n) if n else None
                    tip = (f"{SYSTEM_NAME[s]}, {cond}: {k} of {n} ({pct(v['est'])}%, "
                           f"95% CI {pct(lo)} to {pct(hi)})"
                           + (", correct by construction" if hollow else ""))
                    sc = c(s)
                    whisker = (f'<line x1="{x_of(lo):.1f}" x2="{x_of(hi):.1f}" y1="{cy:.1f}" '
                               f'y2="{cy:.1f}" stroke="{sc}" stroke-width="2" stroke-linecap="round"/>'
                               if hi - lo > 0.005 else "")
                    marks.append(f'<g><title>{escape(tip)}</title>{whisker}'
                                 f'{marker(s, x_of(v["est"]), cy, hollow, inline)}</g>')
                y += sub_h
            y += GAP
    bottom = y - GAP + 2
    grid = []
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        x = x_of(t)
        for y0, y1 in blocks:
            grid.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y0 - 1:.1f}" y2="{y1 + 1:.1f}" '
                        f'stroke="{c("rule")}" stroke-width="1"/>')
        anchor = "middle"
        grid.append(f'<text x="{x:.1f}" y="{bottom + 14:.1f}" text-anchor="{anchor}" class="f1a" '
                    f'fill="{c("ink2")}">{int(t * 100)}</text>')
    y = bottom + 34
    leg = []
    xs = LEFT
    for s in systems:
        leg.append(marker(s, xs + 5, y - 4, False, inline))
        leg.append(f'<text x="{xs + 14}" y="{y}" class="f1a" fill="{c("ink")}">{s}</text>')
        xs += 46
    if "R1" in systems:
        leg.append(marker("R1", xs + 5, y - 4, True, inline))
        leg.append(f'<text x="{xs + 14}" y="{y}" class="f1a" fill="{c("ink")}">by construction</text>')
    y += 10
    h = y + 4
    bg = "" if inline else f'<rect width="{W}" height="{h:.0f}" fill="{LIGHT["bg"]}"/>'
    style = ("" if inline else
             "<style>text{font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,"
             "sans-serif}.f1t{font-size:13.5px}.f1g{font-size:14px}.f1l{font-size:13.5px}"
             ".f1a{font-size:12.5px}</style>")
    label = "Figure 1: percent correct by condition and system" + ("" if score else " (empty until the test run)")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h:.0f}" '
            f'role="img" aria-label="{escape(label)}" class="figure1">{style}{bg}'
            + "".join(grid) + "".join(rows) + "".join(marks) + "".join(leg) + "</svg>")


def table_rows(score: dict) -> list[dict]:
    acc = score["accuracy_main"]
    out = []
    for s in TABLE_SYSTEMS:
        if s not in acc["systems"]:
            continue
        for _, conds in GROUPS:
            for cond in conds:
                v = acc["systems"][s]["by_condition"].get(cond)
                n = acc["n_cases"].get(cond)
                if not v or v.get("est") is None:
                    continue
                lo, hi = v["ci"]
                out.append({"system": s, "condition": cond, "n": n, "k": round(v["est"] * n),
                            "est": round(v["est"], 4), "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                            "by_construction": cond in BY_CONSTRUCTION.get(s, set())})
        m = acc["systems"][s]["macro"]
        out.append({"system": s, "condition": "macro", "n": None, "k": None,
                    "est": round(m["est"], 4), "ci_lo": round(m["ci"][0], 4) if m["ci"][0] is not None else None,
                    "ci_hi": round(m["ci"][1], 4) if m["ci"][1] is not None else None,
                    "by_construction": False})
    return out


def to_png(svg: Path, png: Path, scale: int = 3) -> bool:
    """Rasterize with ffmpeg's librsvg decoder at `scale` x the viewBox size."""
    if not shutil.which("ffmpeg"):
        return False
    text = svg.read_text()
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    sized = svg.with_suffix(".sized.svg")
    sized.write_text(text.replace("<svg ", f'<svg width="{int(m.group(1)) * scale}" '
                                  f'height="{int(m.group(2)) * scale}" ', 1))
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(sized),
                            "-frames:v", "1", str(png)], capture_output=True, text=True)
    finally:
        sized.unlink(missing_ok=True)
    return r.returncode == 0 and png.exists()


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--score")
    g.add_argument("--empty", action="store_true")
    ap.add_argument("--out-dir", default="site/figures")
    ap.add_argument("--name", default=None, help="file stem (default figure1, or figure1_empty)")
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    a = ap.parse_args()
    out = Path(a.out_dir)
    out = out if out.is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    score = None if a.empty else json.loads(Path(a.score).read_text())
    stem = a.name or ("figure1_empty" if a.empty else "figure1")
    systems = tuple(s for s in a.systems.split(",") if s)
    svg = out / f"{stem}.svg"
    svg.write_text(render(score, inline=False, systems=systems) + "\n")
    made = [svg.name]
    if to_png(svg, out / f"{stem}.png"):
        made.append(f"{stem}.png")
    if score:
        rows = table_rows(score)
        with open(out / f"{stem}_table.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        made.append(f"{stem}_table.csv")
        src = f"{score.get('case_set')} / {score.get('run')}"
    else:
        src = "no data"
    print(f"{', '.join(made)} in {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} ({src})")


if __name__ == "__main__":
    main()
