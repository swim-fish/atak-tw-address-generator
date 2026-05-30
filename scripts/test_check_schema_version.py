#!/usr/bin/env python3
"""Self-test for check_schema_version.py.

Builds synthetic project roots (consistent + several drifted variants) in a
temp dir and asserts the checker's verdict for each. Pure stdlib; no pytest.

    python3 scripts/test_check_schema_version.py
"""
from __future__ import annotations

import importlib.util
import io
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "check_schema_version.py"

_spec = importlib.util.spec_from_file_location("check_schema_version", MODULE_PATH)
csv_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_schema_version"] = csv_mod  # let @dataclass resolve __module__
_spec.loader.exec_module(csv_mod)
Checker = csv_mod.Checker


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sqlite_with_version(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)", (version,)
    )
    conn.commit()
    conn.close()


def build_root(base: Path, **o) -> Path:
    """Create a minimal project tree. Override any field to inject drift."""
    g = lambda k, d: o.get(k, d)
    root = base
    _write(root / "scripts/ingest_tgos_csv.py", f'SCHEMA_VERSION = "{g("places", "3")}"\n')
    _write(root / "scripts/extract_places_osm.py", f'SCHEMA_VERSION = "{g("osm", "3")}"\n')
    _write(root / "scripts/migrate_fts_add_area.py", f'TARGET_VERSION = "{g("migrate", "3")}"\n')
    _write(root / "scripts/extract_townships.py", f'    "schema_version": "{g("townships", "1")}",\n')
    _write(root / "scripts/extract_roads.py", f'    "schema_version": "{g("roads", "1")}",\n')

    changelog = "".join(f"### `{n}` — note\n\n" for n in g("changelog", ["3", "2", "1"]))
    _write(
        root / "docs/data-contract.md",
        f"| **Contract version** | `{g('dc_contract', '3')}` (this document) |\n\n"
        f"### 3.1 `places-*.sqlite` (v{g('dc_31', '3')})\n\n"
        f"| `schema_version` | `'{g('dc_meta', '3')}'` | Plugins MUST read this |\n\n"
        f"{changelog}",
    )
    _write(
        root / "README.md",
        f"data%20schema-v{g('readme_badge', '3')}-blue\n\n"
        f"## Data schema — currently **v{g('readme_curr', '3')}**\n",
    )
    _write(
        root / "docs/address-search-guide.md",
        f"> Applies to **schema_version ≥ {g('guide', '3')}**.\n",
    )

    if g("build_output", True):
        out = root / "output"
        _sqlite_with_version(out / "places-taichung.sqlite", g("out_places", "3"))
        _sqlite_with_version(out / "places-osm.sqlite", g("out_places", "3"))
        _sqlite_with_version(out / "townships.sqlite", g("out_townships", "1"))
        _sqlite_with_version(out / "roads.sqlite", g("out_roads", "1"))
    return root


def run(root: Path) -> tuple[int, int, int]:
    """Run the checker silently; return (exit_code, errors, warnings)."""
    checker = Checker(root, root / "output")
    with redirect_stdout(io.StringIO()):
        code = checker.run()
    errors = sum(1 for f in checker.findings if not f.ok and f.severity == "error")
    warns = sum(1 for f in checker.findings if not f.ok and f.severity == "warn")
    return code, errors, warns


CASES = [
    # (name, overrides, expected_exit, expect_errors>0?, expect_warns>0?)
    ("consistent baseline", {}, 0, False, False),
    ("README badge drift (v2)", {"readme_badge": "2"}, 1, True, False),
    ("data-contract §3.1 drift", {"dc_31": "2"}, 1, True, False),
    ("metadata example drift", {"dc_meta": "2"}, 1, True, False),
    ("missing CHANGELOG entry", {"changelog": ["2", "1"]}, 1, True, False),
    ("producers disagree", {"osm": "2"}, 1, True, False),
    ("migration target drift", {"migrate": "2"}, 1, True, False),
    ("output places file drift", {"out_places": "2"}, 1, True, False),
    ("output townships drift", {"out_townships": "2"}, 1, True, False),
    ("guide min-version drift (warn only)", {"guide": "2"}, 0, False, True),
    ("no built artifacts (skip)", {"build_output": False}, 0, False, False),
    ("anchor removed (§3.1 no digit)", {"dc_31": "?"}, 1, True, False),
]


def main() -> int:
    passed = failed = 0
    for i, (name, overrides, exp_code, exp_err, exp_warn) in enumerate(CASES):
        with tempfile.TemporaryDirectory() as td:
            root = build_root(Path(td) / f"case{i}", **overrides)
            code, errors, warns = run(root)
        ok = (
            code == exp_code
            and (errors > 0) == exp_err
            and (warns > 0) == exp_warn
        )
        passed += ok
        failed += not ok
        mark = "PASS" if ok else "FAIL"
        print(
            f"[{mark}] {name.ljust(36)} "
            f"exit={code} (exp {exp_code})  err={errors}  warn={warns}"
        )
    print(f"\n{passed}/{len(CASES)} cases passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
