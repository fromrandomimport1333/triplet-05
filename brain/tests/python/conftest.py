import csv
import importlib.util
import pathlib

# Skip this folder when the citybrain library is not built, so that
# `python -m pytest` still runs the other suites (see src/docs/build.md).
if importlib.util.find_spec("citybrain") is None:
    collect_ignore_glob = ["test_*.py"]

VECTORS_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "vectors" / "golden.csv"
)


def load_golden_sections():
    """Parse vectors/golden.csv into {section_name: [dict, ...]}. Same file
    and format the C test suite reads, so both sides check against the same
    numbers."""
    sections = {}
    current = None
    header = None
    with open(VECTORS_PATH, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                header = None
                continue
            if row[0].startswith("#SECTION:"):
                current = row[0][len("#SECTION:"):]
                sections[current] = []
                header = None
                continue
            if row[0].startswith("#"):
                continue
            if header is None:
                header = row
                continue
            sections[current].append(dict(zip(header, row)))
    return sections
