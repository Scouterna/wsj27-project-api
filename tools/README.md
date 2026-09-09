# tools/

Developer tooling. Not part of the app and excluded from the image
(see `.dockerignore`) — nothing here is imported by `src/`.

## `decode_participants.py`

An offline twin of the app's forms decoder. It reads the cached Scoutnet data in
`src/.dev_cache/` and the same `src/app/forms_template.json` the app uses, so it
decodes all ~2600 participants with no running app and no Scoutnet key.

```bash
python3 tools/decode_participants.py
```

Two jobs:

**1. A safety net for decoder changes.** Its output must stay identical to
`src/app/scoutnet_forms.py`. That equivalence is how a change gets checked
against every member before it ships, rather than against the handful you happen
to look at. To verify:

```python
from app import scoutnet_forms as sf          # with src/ on sys.path
app_out    = sf._build_forms_data(p["questions"], qdefs, templates, Counter())
script_out = json.load(open("tools/generated/decoded_participants.json"))[...]["forms_data"]
assert app_out == script_out
```

It also runs a stricter template validation than the app does (the app only
logs, so a bad template can't take the API down): unknown keys, an id belonging
to a different key, an id listed twice, a key spread across two sections, and
questions in a form that the template omits.

**2. Generating the GUI developer's files**, into `tools/generated/`:

| file | what it is |
|---|---|
| `question_labels.json` | `{key: "original Swedish question"}` — the display lookup, straight from the template |
| `questionKeys.ts` | the same as a typed TS module: `QuestionKey` union, `QUESTION_LABELS`, and the `FormsData` / `Participant` types |
| `question_key_map.json` | every question id in both forms with its key, tab and section — the full audit reference, including questions left out of `forms_data` |
| `decoded_participants.json` | every participant decoded. **Personal data — gitignored, never commit or share.** |

The three small files are safe to hand over; they contain only question text.

## `build_cmt_roles.py`

Builds `cmt-roles.csv` (repo root — what `CMT_ROLES_FILE` points at, see
`src/app/roles.py`) from two inputs that don't share a key:

- `CMT-listan.csv` (repo root) — Kansliet's roster: name + Funktion/Roll, no
  member number.
- `src/.dev_cache/92fa15301e2df7c8.json` — the cached Scoutnet participants
  response, name + member number.

```bash
python3 tools/build_cmt_roles.py
```

Matches by name, which is the risky part — a wrong match hands one person's
CMT detail role to someone else. A roster row is only ever written
automatically when exactly one participant matches it: first on a
whitespace-normalized exact match, falling back to accent/case-folded only if
that finds nobody. Zero or multiple candidates means the row is left out and
printed for manual resolution instead of guessed at.

Also flags, without excluding, rows that would currently have no effect: a
match whose Scoutnet application type isn't Kontingentledning (`roles.py`
never reaches the CMT branch for them regardless of this file), and a match
that isn't confirmed or is cancelled (dropped from the app's participant cache
entirely). Rerun whenever `CMT-listan.csv` changes or the dev cache is
refreshed — it fully recreates `cmt-roles.csv` rather than updating it.

## `question_keys.py`

The full question id → short key table for **both** forms, including the fields
that never reach `forms_data` (next of kin, internal staff fields, questions
consumed elsewhere), plus the sets that classify them. The app itself no longer
needs this — `forms_template.json` carries the ids it uses — but it stays the
reference for anything outside `forms_data`, and `decode_participants.py` uses
it to validate the template.

## Regenerating the dev cache

`src/.dev_cache/` is written by the app when run with a Scoutnet key. It holds
names, personal id numbers, emails and health answers for every participant, and
is gitignored and dockerignored. Delete it and re-run the app to refresh; expect
the data to have drifted (see
[docs/scoutnet-data-quirks.md](../docs/scoutnet-data-quirks.md)).
