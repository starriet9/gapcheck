"""Screenshots and the README GIF of the built page (pages/index.html).

Usage:
  python scripts/site_capture.py gif   [--out site/figures/card.gif]
  python scripts/site_capture.py og    [--out site/figures/og.png]
  python scripts/site_capture.py shots --out <dir>     # review screenshots

Needs `playwright-cli` (Microsoft Playwright CLI) and `ffmpeg` on PATH. Not
part of `make reproduce`: the GIF and screenshots are checked in or kept
locally. The page is served from pages/ on 127.0.0.1 (a random free port)
for the duration of the run, because the CLI does not open file: URLs.
Nothing outside this machine is contacted. One named browser session is
opened and closed again; other sessions are left alone.

GIF: the card at phone width (360 CSS px, device scale 2, so 720 px wide),
three beats (original record, edited record, the verifier catch), reduced
motion, same frame size for every beat, looping. Target < 2 MB.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = "claude-gapcheck-gif"
BEATS_MS = (3000, 3000, 4500)


def serve(directory: Path) -> tuple[socketserver.TCPServer, int]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass
    handler = functools.partial(Quiet, directory=str(directory))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def cli(*args: str, cwd: Path) -> str:
    r = subprocess.run(["playwright-cli", f"-s={SESSION}", *args], cwd=cwd,
                       capture_output=True, text=True)
    if r.returncode != 0 or "### Error" in r.stdout:
        raise SystemExit(f"playwright-cli {args[0]} failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout


def run_js(body: str, params: dict, work: Path) -> str:
    """Run `async (page, P) => {body}` through playwright-cli run-code."""
    f = work / "step.js"
    f.write_text(f"async page => {{ const P = {json.dumps(params)};\n{body}\n}}")
    return cli("run-code", f"--filename={f}", cwd=work)


# A fresh context per capture: its own viewport, device scale, color scheme.
CTX = """
const ctx = await page.context().browser().newContext({
  viewport: { width: P.width, height: P.height }, deviceScaleFactor: P.scale,
  colorScheme: P.scheme, reducedMotion: 'reduce' });
const pg = await ctx.newPage();
await pg.goto(P.url, { waitUntil: 'load' });
"""

GIF_JS = CTX + """
await pg.addStyleTag({ content: 'header.top, footer, main > :not(#card), .card-note, details.all' +
  ' { display: none !important; } main { padding-top: 10px; }' });
const card = pg.locator('#card');
const heights = [];
for (let i = 0; i < P.states; i++) {
  await pg.evaluate(n => window.gapcheckCard.show(n), i);
  heights.push((await card.boundingBox()).height);
}
const h = Math.ceil(Math.max(...heights)) + 16;
await pg.setViewportSize({ width: P.width, height: Math.max(P.height, h + 200) });
const box0 = await card.boundingBox();
for (let i = 0; i < P.states; i++) {
  await pg.evaluate(n => window.gapcheckCard.show(n), i);
  await pg.waitForTimeout(150);
  const b = await card.boundingBox();
  await pg.screenshot({ path: P.dir + '/frame' + i + '.png', fullPage: true,
    clip: { x: 0, y: b.y - 8, width: P.width, height: h } });
}
await ctx.close();
return JSON.stringify({ heights, h });
"""

SHOTS_JS = CTX + """
const total = await pg.evaluate(() => document.documentElement.scrollHeight);
const out = [];
if (P.full) {
  await pg.screenshot({ path: P.prefix + '_full.png', fullPage: true });
  out.push(P.prefix + '_full.png');
}
for (let y = 0, k = 1; y < total && k <= P.maxScreens; y += P.height, k++) {
  await pg.evaluate(v => window.scrollTo(0, v), y);
  await pg.waitForTimeout(80);
  const p = P.prefix + '_screen' + String(k).padStart(2, '0') + '.png';
  await pg.screenshot({ path: p });
  out.push(p);
}
const sw = await pg.evaluate(() => document.documentElement.scrollWidth);
const states = [];
if (P.cardStates) {
  for (let i = 0; i < P.cardStates; i++) {
    await pg.evaluate(n => window.gapcheckCard.show(n), i);
    const p = P.prefix + '_card' + (i + 1) + '.png';
    await pg.locator('#card').screenshot({ path: p });
    states.push(p);
  }
}
await ctx.close();
return JSON.stringify({ total, scrollWidth: sw, width: P.width, files: out.concat(states) });
"""


def ffmpeg_gif(frames: list[Path], beats_ms, out: Path, work: Path) -> None:
    lst = work / "frames.txt"
    lines = []
    for f, ms in zip(frames, beats_ms):
        lines += [f"file '{f.name}'", f"duration {ms / 1000:.2f}"]
    lines.append(f"file '{frames[-1].name}'")  # concat demuxer needs the last file twice
    lst.write_text("\n".join(lines) + "\n")
    pal = work / "palette.png"
    common = ["-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst)]
    subprocess.run(["ffmpeg", "-y", *common, "-vf", "palettegen=max_colors=64:stats_mode=full",
                    str(pal)], check=True, cwd=work)
    subprocess.run(["ffmpeg", "-y", *common, "-i", str(pal), "-lavfi",
                    "paletteuse=dither=none", "-loop", "0", str(out)], check=True, cwd=work)
    if shutil.which("gifsicle"):
        subprocess.run(["gifsicle", "-O3", "-b", str(out)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("gif", "og", "shots"))
    ap.add_argument("--out")
    ap.add_argument("--pages", default="pages")
    a = ap.parse_args()
    for tool in ("playwright-cli", "ffmpeg"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")
    pages = ROOT / a.pages
    if not (pages / "index.html").exists():
        raise SystemExit("pages/index.html not found: run scripts/site_build.py first")
    srv, port = serve(pages)
    base = f"http://127.0.0.1:{port}/index.html"
    work = Path(tempfile.mkdtemp(prefix="gapcheck_capture_"))
    try:
        cli("open", "about:blank", cwd=work)
        if a.what == "gif":
            out = Path(a.out or ROOT / "site" / "figures" / "card.gif").resolve()
            r = run_js(GIF_JS, {"url": base + "?static", "width": 360, "height": 800, "scale": 2,
                                "scheme": "light", "states": len(BEATS_MS), "dir": str(work)}, work)
            frames = [work / f"frame{i}.png" for i in range(len(BEATS_MS))]
            ffmpeg_gif(frames, BEATS_MS, out, work)
            print(f"{out}: {out.stat().st_size / 1024:.0f} KB; {r.strip().splitlines()[-1] if r else ''}")
        elif a.what == "og":
            out = Path(a.out or ROOT / "site" / "figures" / "og.png").resolve()
            run_js(CTX + "await pg.screenshot({ path: P.out }); await ctx.close(); return 'ok';",
                   {"url": base + "?static", "width": 1200, "height": 630, "scale": 1,
                    "scheme": "light", "out": str(out)}, work)
            print(out)
        else:
            # the browser runs in another working directory: paths must be absolute
            out = Path(a.out or ROOT / "private" / "page_preview").resolve()
            out.mkdir(parents=True, exist_ok=True)
            for name, w, h, scheme, full, cards in (("phone375_light", 375, 812, "light", True, 3),
                                                    ("phone375_dark", 375, 812, "dark", False, 3),
                                                    ("phone360_light", 360, 780, "light", False, 0),
                                                    ("desktop1280_light", 1280, 900, "light", True, 0),
                                                    ("desktop1280_dark", 1280, 900, "dark", False, 0)):
                r = run_js(SHOTS_JS, {"url": base + "?static", "width": w, "height": h, "scale": 1,
                                      "scheme": scheme, "prefix": str(out / name), "full": full,
                                      "maxScreens": 12 if w < 500 else 4, "cardStates": cards}, work)
                info = json.loads(json.loads(r.split("### Result")[1].split("\n")[1]))
                flag = "OK" if info["scrollWidth"] <= info["width"] else "HORIZONTAL SCROLL"
                print(f"{name}: page height {info['total']} px, scrollWidth {info['scrollWidth']} "
                      f"({flag}), {len(info['files'])} files")
    finally:
        try:
            cli("close", cwd=work)
        except SystemExit:
            pass
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
