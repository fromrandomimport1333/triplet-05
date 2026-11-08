"""Build docs/report.pdf (Phase 1 report, five parts, 20 pages max) from the Markdown sources.

    python docs/report/build_report.py            # from the repository root (needs: pip install markdown)

Sources: INTEGRATION.md (part 0), docs/report/part2-city-brain.md (part 2), the scenario files and
their latest run logs (part 4, summary; the full Test Book is TESTBOOK.md), docs/report/part5-collaboration.md.
Parts 1 and 3 are written by the Twin and Guardian teams: docs/report/part1-twin.md and
docs/report/part3-guardian.md are used if present. The PDF is printed by Edge or Chrome (headless).
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import markdown
import yaml

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "report"
OUT = ROOT / "docs" / "report.pdf"
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome", "chromium", "microsoft-edge",
]

CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
body { font-family: Calibri, 'Segoe UI', Arial, sans-serif; font-size: 10.5pt; line-height: 1.38; color: #1b2433; }
h1 { font-size: 19pt; color: #0b1626; margin: 0 0 8pt; padding-bottom: 4pt; border-bottom: 2px solid #2dd4bf; }
h2 { font-size: 13pt; color: #0b1626; margin: 12pt 0 4pt; }
h3 { font-size: 11pt; margin: 8pt 0 3pt; }
p { margin: 4pt 0; } ul { margin: 3pt 0 3pt 16pt; padding: 0; } li { margin: 1.5pt 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 9pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #cfd8e3; padding: 3pt 5pt; text-align: left; vertical-align: top; }
th { background: #eef3f8; }
code { font-family: Consolas, 'Courier New', monospace; font-size: 8.6pt; background: #f2f5f9; padding: 0 2pt; }
pre { background: #f2f5f9; padding: 6pt; font-size: 7.2pt; line-height: 1.2; overflow: hidden; white-space: pre; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: inherit; }
.part { page-break-before: always; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover h1 { font-size: 34pt; border: none; margin-bottom: 4pt; }
.cover .sub { font-size: 15pt; color: #0f766e; margin-bottom: 26pt; }
.cover table { width: 70%; font-size: 10.5pt; }
.placeholder { border: 1px dashed #94a3b8; padding: 12pt; color: #475569; }
"""


def md(text: str) -> str:
    return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])


def strip_title(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[1:] if lines and lines[0].startswith("# ") else lines)


def part(title: str, body_md: str) -> str:
    return f'<section class="part"><h1>{title}</h1>{md(body_md)}</section>'


def optional_part(title: str, file: Path, team: str) -> str:
    if file.is_file():
        return part(title, strip_title(file.read_text(encoding="utf-8")))
    return (f'<section class="part"><h1>{title}</h1><p class="placeholder">Written by the {team} team '
            f'(<code>{file.relative_to(ROOT).as_posix()}</code>, five pages, same six subheadings as part 2: '
            f'role and goals, architecture, tech stack and why, interfaces in/out, security measures, known limitations).</p></section>')


def latest_result(sid: str) -> str:
    logs = sorted(glob.glob(str(ROOT / "logs" / f"{sid}-*.log")))
    if not logs:
        return "not run"
    with open(logs[-1], encoding="utf-8") as f:
        last = json.loads(f.readlines()[-1])
    return f"**{last.get('result', '?')}** ({Path(logs[-1]).name})"


def test_book_summary() -> str:
    rows = ["| ID | Scenario | Layers | Expected (Brain) | Result |", "|---|---|---|---|---|"]
    for f in sorted((ROOT / "scenarios").glob("*.yaml"), key=lambda p: (p.stem[0], int("".join(c for c in p.stem if c.isdigit()) or 0))):
        sc = yaml.safe_load(f.read_text(encoding="utf-8"))
        if sc.get("alias_of"):
            continue
        brain = " ".join(str((sc.get("expected") or {}).get("brain", "")).split())
        rows.append(f"| {sc['id']} | {sc['name']} | {', '.join(sc.get('layers', []))} | {brain} | {latest_result(sc['id'])} |")
    intro = ("Each scenario starts with one command, `python scenarios/run.py <ID>`, and writes one log file in "
             "`logs/` with every message observed and the result of each expectation (R2). The full Test Book, "
             "with trigger, expected behaviour of each layer and how to verify, is `TESTBOOK.md`. The organizer's "
             "example T5 (fake flood at the port) is scenario B3.\n\n")
    return intro + "\n".join(rows)


TEAM = os.environ.get("TEAM_NAME", "triplet-05")    # the triplet's name on the cover (one place to change it)


def cover() -> str:
    today = dt.date.today().strftime("%d %B %Y")
    return f"""<section class="cover"><h1>City Brain Convergence</h1>
<div class="sub">{TEAM} · Phase 1 report · TSYP14 technical challenge · {today}</div>
<table><tr><th>Part</th><th>Content</th></tr>
<tr><td>0</td><td>Triplet overview and integration</td></tr>
<tr><td>1</td><td>The City (Twin)</td></tr><tr><td>2</td><td>The City Brain</td></tr>
<tr><td>3</td><td>The Guardian</td></tr><tr><td>4</td><td>Test Book</td></tr>
<tr><td>5</td><td>Collaboration process</td></tr></table></section>"""


def browser() -> str:
    for b in BROWSERS:
        if os.path.isfile(b) or shutil.which(b):
            return b
    sys.exit("no Edge or Chrome found to print the PDF")


def main() -> int:
    html = "".join([
        cover(),
        part("Part 0: Triplet overview and integration", strip_title((ROOT / "INTEGRATION.md").read_text(encoding="utf-8"))),
        optional_part("Part 1: The City (Twin)", REPORT / "part1-twin.md", "Twin"),
        part("Part 2: The City Brain", strip_title((REPORT / "part2-city-brain.md").read_text(encoding="utf-8"))),
        optional_part("Part 3: The Guardian", REPORT / "part3-guardian.md", "Guardian"),
        part("Part 4: Test Book", test_book_summary()),
        part("Part 5: Collaboration process", strip_title((REPORT / "part5-collaboration.md").read_text(encoding="utf-8"))),
    ])
    page = f"<!doctype html><html><head><meta charset='utf-8'><title>City Brain Convergence - Phase 1 report</title><style>{CSS}</style></head><body>{html}</body></html>"
    tmp = REPORT / "_report.html"
    tmp.write_text(page, encoding="utf-8")
    subprocess.run([browser(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={OUT}", tmp.resolve().as_uri()], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    tmp.unlink()
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
