"""Generate testbook.tex (Part 4) from scenarios/*.yaml.  Run from anywhere: python make_testbook_tex.py"""
import glob, re
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[3]
LAY = {"twin": "T", "guardian": "G", "brain": "B"}

def esc(s):
    s = re.sub(r"\s+", " ", str(s)).strip()
    for a, b in [("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("_", r"\_"), ("#", r"\#"), ("$", r"\$"), ("{", r"\{"), ("}", r"\}")]:
        s = s.replace(a, b)
    return s.replace("->", "$\\rightarrow$")

def order(i):
    m = re.match(r"([A-Z])(\d+)", i)
    return (m.group(1) != "B", int(m.group(2)))

rows = []
for f in glob.glob(str(ROOT / "scenarios" / "[BT]*.yaml")):
    d = yaml.safe_load(open(f, encoding="utf-8"))
    if "id" not in d or "expected" not in d:
        continue
    rows.append(d)
rows.sort(key=lambda d: order(d["id"]))
out = [r"\begin{table*}[t]", r"\centering", r"\caption{Test Book: scenarios and expected Brain behaviour (full Test Book: \texttt{TESTBOOK.md}; one log per run in \texttt{logs/}).}",
       r"\label{tab:testbook}", r"\footnotesize", r"\renewcommand{\arraystretch}{1.08}",
       r"\begin{tabular}{@{}l p{4.2cm} c p{9.3cm} r@{}}", r"\toprule",
       r"\textbf{ID} & \textbf{Scenario} & \textbf{Layers} & \textbf{Expected (Brain)} & \textbf{s} \\", r"\midrule"]
for d in rows:
    lay = "".join(LAY[x] for x in d["layers"])
    exp = d["expected"].get("brain") or next(iter(d["expected"].values()))
    out.append(f"{esc(d['id'])} & {esc(d['name'])} & {lay} & {esc(exp)} & {d['duration_s']} \\\\")
out += [r"\bottomrule", r"\end{tabular}", r"\par\smallskip{\scriptsize T = Twin, G = Guardian, B = City Brain; s = run length in seconds.}", r"\end{table*}"]
(Path(__file__).parent / "testbook.tex").write_text("\n".join(out) + "\n", encoding="utf-8")
print(len(rows), "scenarios")
