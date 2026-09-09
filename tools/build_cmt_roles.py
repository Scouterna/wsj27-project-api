"""Build cmt-roles.csv from the hand-maintained CMT roster and the Scoutnet cache.

CMT-listan.csv is Kansliet's roster: one row per CMT member with their name and
their Funktion/Roll, no member number. cmt-roles.csv is what the app actually
reads (CMT_ROLES_FILE, see src/app/roles.py) and needs the opposite key: member
number to Funktion/Roll. This script bridges the two by matching names against
src/.dev_cache/92fa15301e2df7c8.json, the cached Scoutnet participants response
for this project (see tools/README.md - regenerate it by running the app with a
Scoutnet key if it's missing or stale).

Name matching is the risky part: a wrong match hands one person's CMT detail
role to someone else. So a name is only ever resolved automatically when
exactly one participant matches it - on a whitespace-normalized exact match
first, then ROSTER_NAME_ALIASES below for the handful of people Kansliet
writes under a shorter name than Scoutnet has, then falling back to a case-
and accent-folded match. Anything still with zero or more than one candidate
is left out of the output entirely and reported, for a human to resolve by
hand.

Also cross-checked against two things the CSV can't express: whether Scoutnet
itself has the matched person's application type as Kontingentledning (without
that, roles_for_participant() never reaches the CMT branch at all, no matter
what this file says), and whether they are confirmed/not cancelled (the app
drops unconfirmed and cancelled participants from the cache entirely, so an
entry for one is inert until they confirm).

    python3 tools/build_cmt_roles.py
"""

import csv
import json
import re
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROSTER_FILE = REPO / "CMT-listan.csv"
PARTICIPANTS_CACHE = REPO / "src" / ".dev_cache" / "92fa15301e2df7c8.json"
OUTPUT_FILE = REPO / "cmt-roles.csv"

# 84942 (Deltagare/IST form) / 90951 (Avdelningsledare/Kontingentledning form):
# "Typ av ansökan"/"Typ av anmälan". Kept in sync by hand with the same map in
# src/app/scoutnet_forms.py - see docs/scoutnet-data-quirks.md if it drifts.
APPLICATION_TYPE_MAP = {
    "58000": "Deltagare",
    "57999": "IST",
    "62319": "Avdelningsledare",
    "64031": "Kontingentledning",
}
MEMBER_TYPE_CMT = "Kontingentledning"

# CMT-listan.csv name -> the full name Scoutnet has, for people Kansliet writes
# under a shortened name (a dropped middle name, so far) that neither the exact
# nor the folded pass can bridge on its own. Each entry here was confirmed by
# hand against a member number known correct from an earlier roster - add to
# this only after the same kind of check, never as a guess, since a wrong
# alias is exactly the silent misgrant this script exists to avoid.
ROSTER_NAME_ALIASES = {
    "Aud J Bengtsson": "Aud Johanne Bengtsson",
    "Anders Wilson": "Anders Wils Wilson",
}


def normalize(name: str) -> str:
    """Collapse whitespace only - keeps case and accents, for the exact pass."""
    return re.sub(r"\s+", " ", name.strip())


def fold(name: str) -> str:
    """Case- and accent-insensitive form, for the fallback pass only."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", ascii_name.strip()).lower()


def load_roster(path: Path) -> list[dict[str, str]]:
    """CMT-listan.csv, skipping the title row and the blank row above the header.

    Finds the header by content ("Namn" in the first column) rather than by a
    fixed line number, since Kansliet may add or remove title rows.
    """
    rows = list(csv.reader(path.open(encoding="utf-8")))
    header_idx = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Namn")
    header = rows[header_idx]
    return [dict(zip(header, r)) for r in rows[header_idx + 1 :] if any(c.strip() for c in r)]


def load_participant_index(path: Path) -> tuple[dict[str, list[int]], dict[str, list[int]], dict[int, dict]]:
    """Two name -> [member_no] indexes (exact and folded) plus member_no -> record.

    A name mapping to more than one member number is a real, if rare, state in
    a roster this size (nine duplicate full names among ~2800 participants as
    of 2026-09) - both indexes keep every candidate rather than the first one,
    so match_roster() can tell "one match" from "ambiguous" instead of silently
    picking one.
    """
    cache = json.loads(path.read_text(encoding="utf-8"))
    exact_index: dict[str, list[int]] = {}
    fold_index: dict[str, list[int]] = {}
    by_member_no: dict[int, dict] = {}
    for record in cache["participants"].values():
        member_no = record["member_no"]
        full_name = normalize(f"{record['first_name']} {record['last_name']}")
        exact_index.setdefault(full_name, []).append(member_no)
        fold_index.setdefault(fold(full_name), []).append(member_no)
        by_member_no[member_no] = record
    return exact_index, fold_index, by_member_no


def match_roster(
    roster: list[dict[str, str]],
    exact_index: dict[str, list[int]],
    fold_index: dict[str, list[int]],
) -> tuple[list[dict], list[dict]]:
    """Split the roster into confidently matched rows and everything else.

    Matched rows carry `matched_via`: "exact", "alias" or "fold", so the report
    can call out anything short of a plain exact match even though it is
    included in the output.
    """
    matched = []
    unresolved = []
    for row in roster:
        name = normalize(row["Namn"])

        exact_candidates = exact_index.get(name, [])
        if len(exact_candidates) == 1:
            matched.append({**row, "member_no": exact_candidates[0], "matched_via": "exact"})
            continue
        if len(exact_candidates) > 1:
            unresolved.append({**row, "reason": "ambiguous", "candidates": exact_candidates})
            continue

        if alias := ROSTER_NAME_ALIASES.get(name):
            alias_candidates = exact_index.get(normalize(alias), [])
            if len(alias_candidates) == 1:
                matched.append({**row, "member_no": alias_candidates[0], "matched_via": "alias"})
                continue
            # An alias is a deliberate claim about a specific person, so a stale
            # one (Scoutnet no longer has that exact name) is reported as its
            # own reason rather than falling through to the folded pass, which
            # could otherwise "fix" it by matching some other similar name.
            unresolved.append({**row, "reason": f"alias {alias!r} not found", "candidates": alias_candidates})
            continue

        fold_candidates = fold_index.get(fold(name), [])
        if len(fold_candidates) == 1:
            matched.append({**row, "member_no": fold_candidates[0], "matched_via": "fold"})
        elif len(fold_candidates) > 1:
            unresolved.append({**row, "reason": "ambiguous", "candidates": fold_candidates})
        else:
            unresolved.append({**row, "reason": "not found", "candidates": []})
    return matched, unresolved


def sanity_flags(record: dict) -> list[str]:
    """Reasons a matched member's cmt-roles.csv row would currently be inert.

    None of these block writing the row - Funktion/Roll is still correct once
    the underlying Scoutnet state catches up - but each means the row has no
    effect yet, which is worth knowing before assuming this run "did nothing".
    """
    flags = []
    questions = record["questions"]
    application_type = questions.get("84942") or questions.get("90951") or ""
    member_type = APPLICATION_TYPE_MAP.get(application_type, f"unrecognised type {application_type!r}")
    if member_type != MEMBER_TYPE_CMT:
        flags.append(f"Scoutnet has them as {member_type}, not {MEMBER_TYPE_CMT} - roles.py will grant no CMT role")
    if not record["confirmed"]:
        flags.append("not confirmed in Scoutnet - dropped from the app's participant cache entirely")
    if record["cancelled"]:
        flags.append("cancelled in Scoutnet - dropped from the app's participant cache entirely")
    return flags


def write_output(path: Path, matched: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Medlemsnummer", "Namn", "Funktion", "Roll"])
        for row in matched:
            writer.writerow([row["member_no"], normalize(row["Namn"]), row["Funktion"].strip(), row["Roll"].strip()])


def main() -> None:
    roster = load_roster(ROSTER_FILE)
    exact_index, fold_index, by_member_no = load_participant_index(PARTICIPANTS_CACHE)
    matched, unresolved = match_roster(roster, exact_index, fold_index)

    write_output(OUTPUT_FILE, matched)
    print(f"Wrote {len(matched)} of {len(roster)} roster rows to {OUTPUT_FILE.relative_to(REPO)}")

    not_exact = [row for row in matched if row["matched_via"] != "exact"]
    if not_exact:
        print(f"\n{len(not_exact)} matched other than by exact name - double-check these:")
        for row in not_exact:
            via = "a static alias" if row["matched_via"] == "alias" else "folding accents/case"
            print(f"  {row['member_no']}  {normalize(row['Namn'])!r}  (via {via})")

    flagged = []
    for row in matched:
        record = by_member_no[row["member_no"]]
        flags = sanity_flags(record)
        if flags:
            flagged.append((row, flags))
    if flagged:
        print(f"\n{len(flagged)} matched row(s) written, but currently have no effect:")
        for row, flags in flagged:
            print(f"  {row['member_no']}  {normalize(row['Namn'])!r}")
            for flag in flags:
                print(f"    - {flag}")

    if unresolved:
        print(f"\n{len(unresolved)} roster row(s) NOT written - resolve by hand and add manually if needed:")
        for row in unresolved:
            candidates = row["candidates"]
            detail = f"({row['reason']}: {candidates})" if candidates else f"({row['reason']})"
            print(f"  {normalize(row['Namn'])!r} {detail}")


if __name__ == "__main__":
    main()
