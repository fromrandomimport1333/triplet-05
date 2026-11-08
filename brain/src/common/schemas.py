"""Write the JSON Schemas of the models into contracts/schemas/.

    python -m src.common.schemas          # write
    python -m src.common.schemas --check  # fail if a schema file is out of date
"""

from __future__ import annotations

import json
import sys

from .config import BRAIN_ROOT
from .models import EXTERNAL_MODELS, INTERNAL_MODELS

SCHEMA_DIR = BRAIN_ROOT / "contracts" / "schemas"


def rendered() -> dict[str, str]:
    out: dict[str, str] = {}
    external = {f"external-{k.replace('_', '-')}": m for k, m in EXTERNAL_MODELS.items()}
    external.pop("external-guardian-status")          # same model as twin-status
    external["external-status"] = external.pop("external-twin-status")
    for name, model in {**external, **INTERNAL_MODELS}.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        out[f"{name}.schema.json"] = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    return out


def main() -> int:
    files = rendered()
    if "--check" in sys.argv:
        stale = [n for n, text in files.items()
                 if not (SCHEMA_DIR / n).is_file() or (SCHEMA_DIR / n).read_text() != text]
        if stale:
            print("out of date:", ", ".join(stale))
            return 1
        return 0
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (SCHEMA_DIR / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} schemas to {SCHEMA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
