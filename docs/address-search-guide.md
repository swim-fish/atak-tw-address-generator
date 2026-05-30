# Address search guide (downstream / plugin developers)

> Audience: developers querying `places-*.sqlite` (and `tw-central-full.zip`)
> from an ATAK plugin or any offline consumer.
> Applies to **schema_version ≥ 3**. For the full file format see
> [`data-contract.md`](./data-contract.md); for the research behind the v3
> change see [`empty-street-fts-report.md`](./empty-street-fts-report.md).

This guide tells you how to build **forward search** (text → address/coordinate)
queries that actually match, and how addresses **without a 路/街** behave.

---

## 1. The one thing you must know about the tokenizer

`places_fts` uses `tokenize='unicode61'`. **A contiguous run of CJK characters
is a single token — it is NOT one token per character.** `MATCH` compares whole
tokens, not substrings. So:

- `MATCH '"中山路"'` matches **only** if `中山路` is a whole token. It is — but
  only because `street` is indexed as its own short column.
- `MATCH '"大誠里"'` (a 村里 buried inside `display_name`) → **0 hits**, because
  the whole `台中市中區大誠里大誠街…` run is one token.
- Splitting into single chars (`"大 誠 街"`) does **not** help → 0 hits.

> ⚠️ Do not assume per-character CJK tokenization. Earlier revisions of the docs
> claimed it; it is false. Design your queries around whole-token / prefix
> matching, or use the components in §3.

### What is matchable as a whole token

`places_fts` indexes these columns; each gives clean tokens for **its own short
value**:

| FTS column | Whole-token search that works |
|---|---|
| `street` | `"大誠街"`, `"中山路"` |
| `township` | `"中區"`, `"鹿港鎮"` |
| **`area`** (v3+) | `"十甲巷"`, `"介壽新村"` — see §3 |
| `name`, `display_name`, `display_name_halfwidth` | the **exact full string**, or a **prefix from the head** |

---

## 2. Recommended query patterns

### 2.1 Prefix search — the default for a search box

Append `*` to do prefix matching against the head of `name` /
`display_name_halfwidth`. This is the most forgiving pattern and the one to use
for autocomplete.

```sql
-- User typed: 大誠街5   (sanitise first — strip FTS5 punctuation " * ( ) - : ' \)
SELECT p.id, p.display_name, p.lat, p.lon
FROM places p
WHERE p.id IN (SELECT rowid FROM places_fts WHERE places_fts MATCH ?)
LIMIT 50;
-- bind:  大誠街*        (note the trailing *)
```

Rules:
- Strip / escape FTS5 syntax characters from user input before building the
  query: `" * ( ) - : ' \`.
- Build the bound string as `<sanitised>*` for prefix, or `"<sanitised>"` for an
  exact phrase.
- Use **`display_name_halfwidth` / `name`** for matching (ASCII digits), and
  **`display_name`** for display. Convert the user's fullwidth digits to
  halfwidth and `之`→`-` first (mirror `normalize_address.number_with_hyphens`).

### 2.2 Exact phrase — when you have a full address

```sql
SELECT rowid FROM places_fts WHERE places_fts MATCH ?;   -- bind: "台中市東區十甲里十甲巷30弄7號"
```

Matches the single exact row. Useful for de-duplication / round-trip checks, not
for free-text search.

### 2.3 District pre-filter — always cheap, always correct

When you already know the 鄉鎮市區 (e.g. from reverse-geocoding the map centre),
filter by `district_code` first. This narrows the candidate set and lets you
combine with a `LIKE` substring as a fallback for short (≤2-char) fragments that
FTS cannot tokenize:

```sql
SELECT id, display_name, lat, lon
FROM places
WHERE district_code = :code
  AND display_name_halfwidth LIKE '%' || :fragment || '%'   -- app-side, indexed by district
LIMIT 50;
```

`LIKE '%…%'` is a scan, but a single district is ~10–200k rows and runs in
well under a second; keep it as the fallback path, not the primary one.

---

## 3. Addresses with no 路/街 (empty-street / `area`-located)

Some addresses have **no street**. In Taichung ~1.9 % and in Changhua ~10 % of
rows have `street IS NULL`. They are located by the **`area`** field — almost
always a *named lane* (`福上巷`, `廖厝巷`, `岸頭巷`), sometimes a settlement
(`介壽新村`, `中興嶺`). Their `display_name` is composed with `area` in the
street slot, e.g. `彰化縣彰化市介壽里介壽新村１號`.

**Schema v3 makes these findable by their locality name.** `area` is now an
indexed FTS column, so:

```sql
MATCH '"十甲巷"'    -> 15   ✅   (was 0 before v3)
MATCH '"介壽新村"'  -> 372  ✅
MATCH  十甲巷*      -> 15   ✅   (prefix also works)
```

How to handle them in a search box:
- A whole-token query equal to the area name now hits (`"十甲巷"`).
- Prefix (`十甲巷*`) hits via the `name` head as before.
- Reverse geocoding (coordinate → nearest address) has always worked for these
  rows and is unchanged — `display_name` already includes the area.

You do **not** need to special-case empty-street rows in app code; they behave
like any other row for exact-name and prefix search, and their `display_name`
is complete.

---

## 4. What still does NOT work (and when to ask for 2b)

Schema v3 (fix 2a) covers **whole area names and prefixes**. It does **not**
give you:

- **Mid-string substring** of a long field — e.g. searching `大誠里` (a 村里
  embedded mid-`display_name`) still returns 0 via FTS. Use the §2.3
  `district_code` + `LIKE` fallback for these.
- **General "type any fragment, match anywhere"** search. That requires a
  `trigram` index (fix **2b**), which is an additive index on
  `display_name_halfwidth` (+~68 MiB across both counties, ≥3-char queries
  only). It is intentionally deferred — request it if product needs free
  substring search. See [`empty-street-fts-report.md`](./empty-street-fts-report.md) §4.

---

## 5. Schema version handling

```sql
SELECT value FROM metadata WHERE key='schema_version';
```

| Reads | `area` in `places_fts`? | Empty-street exact-name search |
|---|---|---|
| `'1'`, `'2'` | no | only via prefix (`十甲巷*`) or full-string exact |
| `'3'`+ | **yes** | `"十甲巷"` works |

Plugins MUST read `schema_version` and MAY fall back to prefix-only behaviour on
older files. Nothing in v3 is breaking: existing `street`/`township`/full-string
queries return the same results; v3 only **adds** matchable `area` tokens.
