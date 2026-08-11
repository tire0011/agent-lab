"""Writing run artifacts to state/runs/*.json."""

import json
from pathlib import Path
from uuid import uuid4


def log_run(entry: dict, runs_dir: str = "state/runs") -> None:
    path = Path(runs_dir)
    path.mkdir(parents=True, exist_ok=True)
    filename = f"{entry['timestamp'].replace(':', '-').replace('.', '-')}_{uuid4().hex}.json"
    filepath = path / filename
    filepath.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
