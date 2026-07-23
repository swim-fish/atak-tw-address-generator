# Data Versioning and Release Identity

The generator tracks three independent identities:

| Identity | Example | Changes when |
|---|---|---|
| `schema_version` | `3` | SQLite tables, columns, indexes, or consumer contract require a schema migration |
| `data_version` | `2026.07.23.1` | Any data kit is published, including a rebuild from unchanged source inputs |
| `address_policy_version` | `2` | Address normalization, floor consolidation, or deduplication semantics change |

The single source of truth for release-facing versions is
`config/data_version.yaml`. `data_version` uses
`YYYY.MM.DD.REVISION`; the revision starts at `1` and increments for
additional releases on the same date.

Every fresh producer writes `data_version` into SQLite metadata. TGOS
databases also write `address_policy_version`. `build_manifest.py` refuses
to package stale SQLite metadata.

Each release ZIP contains:

```text
timestamp.data-version
timestamp.address-policy-version
```

Each sidecar manifest contains top-level `Data version` and
`Address policy` values, plus the version metadata for each SQLite.

## Release workflow

1. Update `config/data_version.yaml`.
2. Rebuild the affected databases. For an intentional metadata-only
   re-release, review the dry run and explicitly stamp existing artifacts:

   ```bash
   python scripts/stamp_data_version.py
   python scripts/stamp_data_version.py --apply
   ```

3. Package and run the release preflight:

   ```bash
   ./run.sh pack
   ./run.sh check-version
   ```

4. Put the data version, address-policy version, ZIP SHA-256, and per-file
   schema versions in the GitHub release notes.

The SQLite file's own SHA-256 remains outside the database to avoid a
self-referential hash. It is stored in the sidecar manifest and recomputed
by the plugin during import.
