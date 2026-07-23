# Empty-street addresses & the FTS5 tokenizer — research report

> Scope: Taichung City + Changhua County TGOS door-plate data.
> Date: 2026-05-30 · Status: findings that motivate schema v3 (fix **2a**).
> Companion: [`address-search-guide.md`](./address-search-guide.md) (downstream query manual),
> [`data-contract.md`](./data-contract.md) (file format).

## 0. The question

Do Taichung / Changhua have addresses with **no 路 / 街** — i.e. addresses
located only by a number plus a 厝 / 里-style place name? And if such rows
exist, do they survive into `tw-central-full.zip`, and what happens when a
downstream plugin searches for them?

Short answer: **yes, such rows exist and ship**, but the "only number + 厝/里"
mental model is wrong — they are located by the **`地區` (area)** field, which
is overwhelmingly a *named lane* (`…巷`), not a 厝/里. More importantly, the
existing FTS5 index could **not** find them by that locality name, because of a
tokenizer assumption that turns out to be false. This report records the
evidence and the capacity/memory trade-offs of the fix.

---

## 1. How many addresses have no 路/街?

`街、路段` (street) is the field that normally ends in 路/街/段/道. Measuring
the empty-street rate:

| Level | Taichung | Changhua |
|---|---|---|
| Raw CSV rows | 1,316,674 | 467,023 |
| CSV rows with empty `街、路段` | 17,736 (**1.35 %**) | 44,028 (**9.43 %**) |
| May 2026 shipped baseline (pre-address-policy-v2) | 731,005 | 426,690 |
| Shipped rows with `street IS NULL` | 14,021 (**1.92 %**) | 43,090 (**10.10 %**) |

Changhua's empty-street share is ~7× Taichung's. The numbers match the
pre-existing note in `normalize_address.py` (the "orphan" case, 1.3 % / 9.4 %).

**These rows are not dropped.** `ingest_tgos_csv.py` skips a row only when the
`號` is empty, the district code is unknown, or it is listed in
`dirty_data.yaml`. An empty street is none of those, so every empty-street row
with a number reaches `places` and therefore `tw-central-full.zip`. Their
`display_name` is composed via the `area` fallback in
`compose_display_name()`, e.g. `彰化縣彰化市介壽里介壽新村１號`. Coordinates
and the R*Tree entry are intact, so **reverse geocoding (coordinate → address)
works for them unchanged**.

## 2. What actually locates them — `地區`, almost always a named 巷

Of the empty-street rows, virtually all carry a non-empty `地區`
(Taichung 17,716 / 17,736; Changhua 43,985 / 44,028). Classifying the **last
character** of that `地區` value:

| `地區` ends with | Taichung | Changhua | Examples |
|---|---|---|---|
| 巷 (named lane) | 14,604 (82 %) | 42,696 (97 %) | 福上巷、廖厝巷、岸頭巷 |
| 莊 / 新莊 | 1,235 | 282 | 樂群新莊、惠民莊 |
| 新村 | — | 722 | 介壽新村、太極新村 |
| 嶺 / 道 / 坪 / 林 / 新城 … | rest | rest | 中興嶺、府會園道、敬業新城 |

So a "no road/street" address is really `…里 + <named 巷> + 號` (e.g.
`台中市大里區十甲里十甲巷５號`), not `號 + 厝/里`.

The literal "number + 厝" / "number + 里" pattern — a `地區` that *ends* in 厝
or 里 — is negligible:

| `地區` ends with | Taichung | Changhua | All occurrences |
|---|---|---|---|
| 厝 | 0 | 1 | 彰化縣頂庄村**西厝**3號 |
| 里 | 0 | 9 | 彰化縣東港村**宮后里**3/6/8/11…號 |

`廖厝巷`-style values contain 厝 mid-string but end in 巷. Rows with *nothing*
but 村里+號 (no area, no lane/alley) number 5 in Taichung and 8 in Changhua,
and are mostly dirty (empty or malformed `號`).

**Conclusion for §0:** the locality that must be searchable for empty-street
addresses is the `area` field (a named 巷 / 莊 / 新村 / …), not 厝/里.

---

## 3. The tokenizer finding (the important part)

`places_fts` is declared `tokenize='unicode61'`, and both `data-contract.md`
and the operator manual claimed *"unicode61 treats CJK characters as individual
tokens, so phrase queries match contiguously."* **This is false.** `unicode61`
classifies CJK ideographs as alphabetic, so a **contiguous run of CJK becomes a
single token**. Measured against the shipped `places-taichung.sqlite`:

```
MATCH '"大 誠 街"'  (space-separated single chars) -> 0   # would be >0 if per-char
MATCH '"十甲巷"'                                    -> 0   # area name, empty-street row
MATCH '"大誠里"'   (a 村里 / village)               -> 0
MATCH '"大誠街"'   (a street)                       -> 307 ✅
MATCH '"中區"'     (a township)                     -> 4699 ✅
prefix  十甲巷*                                      -> 15  ✅
prefix  大誠*                                        -> 307 ✅
```

The `fts5vocab` index makes the cause concrete — there is no standalone
`十甲巷` token; the area only appears fused inside larger tokens:

```
token '十甲巷30弄7號'   x1     # the `name` column (area+lane+alley+number, halfwidth)
token '台中市東區十甲里十甲巷'   ...   # the display_name CJK run
token '大誠街'           x307   # the `street` column — standalone, because street is its own FTS column
```

Why streets and townships work but area / village do not: `street` and
`township` are **their own FTS columns**, so their short values become whole
tokens (`大誠街`, `中區`). `area` is **not** an indexed column — it only lives,
fused, inside `name` and `display_name(_halfwidth)`. FTS5 `MATCH` is
token-level, not substring, so:

- **Exact / phrase search by area name** (`"十甲巷"`, `"介壽新村"`) → **0 hits.**
- **Prefix search** (`十甲巷*`) → works, because the area sits at the *head* of
  the `name` token.
- **Township search** (`中區`/`北區`) → works but returns thousands.
- The only exact hit is typing the **entire** halfwidth display string
  (`"台中市東區十甲里十甲巷30弄7號"`) → 1 row — which is also exactly what
  `verify_samples.py` check 5 does, hence its 100 % pass rate masked the gap.

This is broader than empty-street rows (village search and any mid-string
substring also fail), but empty-street rows are the worst affected because the
`area` name is their *only* locality identifier and it has no FTS column.

---

## 4. Fix options — measured capacity & runtime cost

Two fixes were prototyped on the real data (minimal `places` copy, external
`content='places'` FTS rebuilt + `VACUUM`, index size = file − base table).

### Current footprint

`places_fts` (unicode61, 5 cols): **Taichung 46.5 MiB, Changhua 27.1 MiB**
(73.6 MiB combined). `tw-central-full.zip` ≈ 142 MiB.

### 2a — add `area` as an FTS column (unicode61)

Makes `area="十甲巷"` a standalone token, so exact area-name phrase search
works (verified: `"十甲巷"` → 0 → **15** after the change).

| | Taichung | Changhua | Combined |
|---|---|---|---|
| Index Δ (disk) | +0.8 MiB | +0.6 MiB | **+1.4 MiB** |
| ZIP Δ | ~0 | ~0 | **~0** |
| Runtime RAM Δ | none | none | **none** |

Fixes: exact 地名 search. Does **not** fix village (`大誠里`) or mid-string
substring.

### 2b — add a `trigram` index on `display_name_halfwidth` (alongside unicode61)

Enables any **≥3-character substring** anywhere in the address (verified:
`大誠里` → 1189, `十甲巷` → 15, mid-string street fragments match).

| | Taichung | Changhua | Combined |
|---|---|---|---|
| Index Δ (disk) | +43.0 MiB | +25.1 MiB | **+68.1 MiB** |
| ZIP Δ | +7.6 MiB | +4.1 MiB | **+11.7 MiB** |
| Per-county sqlite | +14 % | +14 % | — |

Caveats: trigram needs **≥3 chars** (2-char queries like `中區`/`中山` return
0, so unicode61 must stay for short queries and ranking). Runtime memory is
**not** proportional to index size — FTS5 streams doclists; the real cost is a
**historical hot-trigram scan**: the most common trigram `彰化縣` spanned
**all 426,690**
Changhua rows, so a bare 3-char query on a very common trigram is CPU-bound
(MB-scale buffers, not the whole index). Mitigate with ≥3-char minimum,
AND-combined longer queries, and a `district_code` pre-filter.

### Runtime memory, general

FTS5 query memory is governed by `PRAGMA cache_size` (the page cache), **not**
the on-disk index size. A bigger index increases storage and, on a small cache
(Android default ≈ 2 MB), page-cache misses → more I/O, but RAM stays bounded.
2a is effectively free at runtime; 2b raises only transient per-query buffers
for common-trigram queries.

---

## 5. Decision

Implement **2a now** (`area` added to `places_fts`, schema_version 2 → 3):
it directly fixes the reported problem (empty-street addresses findable by
their locality name) at +1.4 MiB / zero runtime cost, and parallels the
existing `street`-as-FTS-column design. Defer **2b (trigram)** until
mid-string / village substring search is a product requirement; it is an
additive, independent index that can be layered on later.

The false "per-character CJK" claim is corrected in `data-contract.md` §5.5,
`atak-tw-address-manual.md` §8, and the `verify_samples.py` check-5 comment.
