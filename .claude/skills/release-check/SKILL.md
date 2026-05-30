---
name: release-check
description: Pre-flight checklist and steps for cutting a GitHub release of the ATAK tw-address data kits (tw-central-full.zip, base.zip, places-*.zip) on swim-fish/atak-tw-address-generator. Use when asked to "create a release", "cut vX.Y.Z", "upload the kit zip", or "publish the manifest".
---

# Release checklist — atak-tw-address-generator data kits

Authoritative procedure for publishing a versioned GitHub release of the SQLite
data kits. Each kit ZIP (`base.zip`, `places-<county>.zip`, `tw-central-full.zip`)
ships with a sidecar `*.manifest.txt`. The release MUST be internally consistent:
the ZIP on GitHub, its `sha256` in the manifest, and the `schema_version` of every
bundled `.sqlite` must all agree, and the release notes must restate them.

**Repo:** `swim-fish/atak-tw-address-generator` · **Asset dir:** `output/`
**gh account:** `swim-fish` (run `gh auth status` first).

## 0. Preconditions

- [ ] `gh auth status` → logged in as `swim-fish`, scope includes `repo`.
- [ ] Kits exist in `output/` (built via `./run.sh all` or `./run.sh pack`).
- [ ] Decide the tag. Check existing ones: `gh release list --repo swim-fish/atak-tw-address-generator`.
      Tags are `vX.Y.Z` (semver). Do not reuse an existing tag.

## 1. Verify ZIP integrity against the manifest

The manifest records `ZIP SHA-256:`. Recompute and compare — never trust a stale manifest.

```bash
cd output
sha256sum tw-central-full.zip          # must equal the manifest's "ZIP SHA-256:"
grep -i 'ZIP SHA-256' tw-central-full.manifest.txt
```

If they differ, the ZIP was rebuilt after the manifest — re-run `./run.sh pack` and stop.

## 2. Read schema_version from the actual SQLite (ground truth)

Do NOT infer versions from source code — read them from the files that are
actually in the ZIP. The value lives in each DB's `metadata` table.

```bash
cd output
for db in townships roads places-osm places-taichung places-changhua; do
  printf '%-18s schema_version=' "$db"
  sqlite3 "$db.sqlite" "SELECT value FROM metadata WHERE key='schema_version';"
done
```

Current expected baseline (bump deliberately, per `docs/data-contract.md`):

| Dataset | schema_version |
|---|:--:|
| townships | 1 |
| roads | 1 |
| places-osm | 3 |
| places-taichung | 3 |
| places-changhua | 3 |

Contract reminder for the notes: plugins MUST branch on `schema_version`; the
`places-*` address-search / FTS `area` contract is **schema_version ≥ 3**.

## 3. Manifest must carry schema_version

`scripts/build_manifest.py` emits `meta.schema_version` per dataset (the key is
included in its meta key list). If a manifest predates that fix and is missing
the line, regenerate with `./run.sh pack`, or — only as a one-off — add
`    meta.schema_version: <N>` under each dataset's `sha256:` line so it matches
step 2. The manifest and the release-notes table must show identical versions.

## 4. Create the release and upload assets

Target the current HEAD commit. Upload both the ZIP and its manifest. Notes
restate region, bbox, ZIP size, ZIP SHA-256, and the per-dataset schema_version
table from step 2 (see `docs/` for the canonical wording).

```bash
gh release create vX.Y.Z \
  --repo swim-fish/atak-tw-address-generator \
  --target "$(git rev-parse HEAD)" \
  --title "vX.Y.Z — tw-central-full" \
  --notes-file /tmp/release-notes.md \
  "output/tw-central-full.zip#tw-central-full.zip" \
  "output/tw-central-full.manifest.txt#tw-central-full.manifest.txt"
```

To amend an existing release instead of creating one:
- Notes: `gh release edit vX.Y.Z --repo ... --notes-file /tmp/release-notes.md`
- Re-upload an asset: `gh release upload vX.Y.Z --repo ... output/<file> --clobber`

## 5. Post-publish verification

```bash
gh release view vX.Y.Z --repo swim-fish/atak-tw-address-generator \
  --json tagName,name,url,assets \
  --jq '{tag:.tagName, url:.url, assets:[.assets[]|{name:.name,size:.size,state:.state}]}'
```

- [ ] Every asset `state` is `uploaded` (not `starting`/`uploading`).
- [ ] `tw-central-full.zip` byte size matches local `ls -la output/tw-central-full.zip`.
- [ ] Notes table schema_versions == step 2 == manifest (step 3).
- [ ] (optional) Update the README version table / changelog if the schema bumped.

## Notes

- `tw-central-full.zip` is ~142 MiB; uploads take a moment — re-run step 5 until
  `state: uploaded` before announcing the release.
- Multiple kits in one release: repeat steps 1–4 for `base.zip` and each
  `places-<county>.zip`, attaching every ZIP + manifest pair.
