#!/usr/bin/env python3
"""check_schema_version.py — schema-version consistency gate.

Run this after any schema bump. It fails (exit 1) if the declared schema
version drifts between the code that emits it, the documents that describe
it, and the sqlite files actually produced.

Two layers:

  STATIC  — the `places` schema version is owned by the two producers
            (ingest_tgos_csv.py, extract_places_osm.py). Their SCHEMA_VERSION
            constant is the single source of truth. The migration script and
            every doc that states "the current places schema version" must
            agree with it. `townships.sqlite` and `roads.sqlite` are
            standalone schemas with their own independent, pinned versions
            (see docs/data-contract.md §3.2 / §3.3) — they are checked
            against STANDALONE_EXPECTED, NOT against the places version.

  OUTPUT  — if built artifacts exist under output/, each file's
            metadata.schema_version is read and compared against the version
            the code claims to write. Skipped cleanly when nothing is built.

Usage:
    python3 scripts/check_schema_version.py
    python3 scripts/check_schema_version.py --root /path/to/generator
    python3 scripts/check_schema_version.py --output-dir /custom/output
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# --- Single source of truth -------------------------------------------------
# Both producers emit the `places` schema; their constants must agree, and
# the first one listed is treated as canonical for all downstream checks.
PLACES_PRODUCERS = [
    "scripts/ingest_tgos_csv.py",
    "scripts/extract_places_osm.py",
]

# Standalone schemas. These are intentionally NOT the places version — they
# version independently. Bump these constants only when the corresponding
# table layout actually changes (and add a data-contract note when you do).
STANDALONE_EXPECTED = {
    "townships.sqlite": "1",
    "roads.sqlite": "1",
}


@dataclass
class Finding:
    ok: bool
    severity: str  # 'error' | 'warn'
    label: str
    detail: str


class Checker:
    def __init__(self, root: Path, output_dir: Path) -> None:
        self.root = root
        self.output_dir = output_dir
        self.findings: list[Finding] = []

    # --- low-level helpers --------------------------------------------------
    def _read(self, rel: str) -> str | None:
        path = self.root / rel
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _extract(self, rel: str, pattern: str, group: int = 1) -> str | None:
        text = self._read(rel)
        if text is None:
            return None
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(group) if m else None

    def _extract_all(self, rel: str, pattern: str, group: int = 1) -> list[str]:
        text = self._read(rel)
        if text is None:
            return []
        return [m.group(group) for m in re.finditer(pattern, text, re.MULTILINE)]

    def _add(self, ok: bool, label: str, detail: str, severity: str = "error") -> None:
        self.findings.append(Finding(ok, severity, label, detail))

    def _expect(
        self,
        label: str,
        rel: str,
        pattern: str,
        expected: str,
        severity: str = "error",
    ) -> None:
        if self._read(rel) is None:
            self._add(False, label, f"file not found: {rel}", severity)
            return
        got = self._extract(rel, pattern)
        if got is None:
            self._add(False, label, f"anchor not found in {rel} — update this checker", severity)
        elif got == expected:
            self._add(True, label, f"{rel}: v{got}")
        else:
            self._add(False, label, f"{rel}: found v{got}, expected v{expected}", severity)

    # --- source of truth ----------------------------------------------------
    def places_version(self) -> str | None:
        canonical_rel = PLACES_PRODUCERS[0]
        canonical = self._extract(canonical_rel, r'SCHEMA_VERSION\s*=\s*"(\d+)"')
        if canonical is None:
            self._add(False, "source of truth", f"SCHEMA_VERSION not found in {canonical_rel}")
            return None
        # All other producers must agree with the canonical constant.
        for rel in PLACES_PRODUCERS[1:]:
            got = self._extract(rel, r'SCHEMA_VERSION\s*=\s*"(\d+)"')
            if got is None:
                self._add(False, "producer agreement", f"SCHEMA_VERSION not found in {rel}")
            elif got != canonical:
                self._add(
                    False,
                    "producer agreement",
                    f"{rel}: v{got} != canonical {canonical_rel}: v{canonical}",
                )
            else:
                self._add(True, "producer agreement", f"{rel} agrees: v{got}")
        return canonical

    # --- static checks ------------------------------------------------------
    def check_static(self, v: str) -> None:
        # Migration script target must match the current places version.
        self._expect(
            "migration target",
            "scripts/migrate_fts_add_area.py",
            r'TARGET_VERSION\s*=\s*"(\d+)"',
            v,
        )

        # Standalone producers emit their own pinned versions.
        self._expect(
            "townships producer",
            "scripts/extract_townships.py",
            r'"schema_version":\s*"(\d+)"',
            STANDALONE_EXPECTED["townships.sqlite"],
        )
        self._expect(
            "roads producer",
            "scripts/extract_roads.py",
            r'"schema_version":\s*"(\d+)"',
            STANDALONE_EXPECTED["roads.sqlite"],
        )

        # docs/data-contract.md — the canonical contract.
        dc = "docs/data-contract.md"
        self._expect(
            "data-contract: contract version",
            dc,
            r"\*\*Contract version\*\*\s*\|\s*`(\d+)`",
            v,
        )
        self._expect(
            "data-contract: §3.1 header",
            dc,
            r"### 3\.1 `places-\*\.sqlite` \(v(\d+)\)",
            v,
        )
        self._expect(
            "data-contract: metadata example",
            dc,
            r"`schema_version`\s*\|\s*`'(\d+)'`",
            v,
        )
        changelog = self._extract_all(dc, r"^### `(\d+)`")
        if not changelog:
            self._add(False, "data-contract: CHANGELOG", f"no `### `N`` entries found in {dc}")
        elif v in changelog:
            self._add(True, "data-contract: CHANGELOG", f"{dc}: has an entry for v{v}")
        else:
            self._add(
                False,
                "data-contract: CHANGELOG",
                f"{dc}: no CHANGELOG entry for current v{v} (found {sorted(set(changelog))})",
            )

        # README.md — the front door.
        rm = "README.md"
        self._expect("README: schema badge", rm, r"data%20schema-v(\d+)", v)
        self._expect("README: 'currently vN'", rm, r"currently \*\*v(\d+)\*\*", v)

        # docs/address-search-guide.md states a minimum applicable version.
        # Semantically a floor (features stay valid on later versions), so a
        # mismatch is a warning, not a hard failure.
        self._expect(
            "address-search-guide: min version",
            "docs/address-search-guide.md",
            r"schema_version\s*≥\s*(\d+)",
            v,
            severity="warn",
        )

    # --- output / built-artifact checks -------------------------------------
    def _read_meta_version(self, path: Path) -> str | None:
        try:
            # `with sqlite3.connect(...)` manages the transaction, NOT the
            # connection — it leaves the file handle open (and on Windows the
            # file locked). Close explicitly.
            uri = f"file:{path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                row = conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            finally:
                conn.close()
            return row[0] if row else None
        except sqlite3.Error as exc:
            return f"<error: {exc}>"

    def _expected_for(self, name: str, places_version: str) -> str:
        if name in STANDALONE_EXPECTED:
            return STANDALONE_EXPECTED[name]
        # places-taichung.sqlite / places-changhua.sqlite / places-osm.sqlite
        return places_version

    def check_output(self, v: str) -> None:
        if not self.output_dir.is_dir():
            self._add(True, "output artifacts", f"none built (no {self.output_dir}) — skipped")
            return
        sqlites = sorted(self.output_dir.rglob("*.sqlite"))
        if not sqlites:
            self._add(True, "output artifacts", f"none built under {self.output_dir} — skipped")
            return
        for path in sqlites:
            name = path.name
            expected = self._expected_for(name, v)
            got = self._read_meta_version(path)
            rel = path.relative_to(self.root) if self.root in path.parents else path
            if got is None:
                self._add(False, f"output: {name}", f"{rel}: no schema_version in metadata")
            elif got.startswith("<error"):
                self._add(False, f"output: {name}", f"{rel}: {got}")
            elif got == expected:
                self._add(True, f"output: {name}", f"{rel}: v{got}")
            else:
                self._add(
                    False,
                    f"output: {name}",
                    f"{rel}: file says v{got}, code emits v{expected}",
                )

    # --- driver -------------------------------------------------------------
    def run(self) -> int:
        v = self.places_version()
        if v is None:
            self._report(None)
            return 2
        self.check_static(v)
        self.check_output(v)
        return self._report(v)

    def _report(self, v: str | None) -> int:
        width = max((len(f.label) for f in self.findings), default=0)
        errors = warns = 0
        if v is not None:
            print(f"Source of truth — places schema version: v{v}")
            print(f"Standalone pins: " + ", ".join(f"{k}=v{val}" for k, val in STANDALONE_EXPECTED.items()))
            print()
        for f in self.findings:
            if f.ok:
                mark = "  OK  "
            elif f.severity == "warn":
                mark = " WARN "
                warns += 1
            else:
                mark = " FAIL "
                errors += 1
            print(f"[{mark}] {f.label.ljust(width)}  {f.detail}")
        print()
        print(f"Summary: {errors} error(s), {warns} warning(s), "
              f"{sum(1 for f in self.findings if f.ok)} ok.")
        return 1 if errors else 0


def main() -> int:
    default_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Schema-version consistency gate.")
    ap.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help="Generator project root (default: parent of scripts/).",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override the built-artifact directory (default: <root>/output).",
    )
    args = ap.parse_args()
    root = args.root.resolve()
    output_dir = (args.output_dir or (root / "output")).resolve()
    return Checker(root, output_dir).run()


if __name__ == "__main__":
    raise SystemExit(main())
