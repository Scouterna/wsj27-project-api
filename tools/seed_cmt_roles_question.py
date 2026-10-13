"""One-time job: copy cmt-roles.csv into Scoutnet question 120008 (#8).

Issue #8 moves the CMT Funktion/Roll mapping out of the hand-kept CSV and into
Scoutnet question 120008 "CMT funktionstillhörighet och roll" (form 47115). This
seeds that question from the CSV once, so the field starts out with what the app
serves today.

The value written is "<Funktion> | <Roll>", e.g. "Support | Hälsa PL": the
labels as they stand in the CSV, so the field stays readable and editable by
hand in Scoutnet, and the app can go on applying _drop_pl_suffix() and _slug()
when it reads them. " | " is the separator because Roll itself can hold a "/"
("FA/CET"); the postal code field uses the same one.

Reads the participants live from Scoutnet (not the dev cache) to check each
member before writing:

  * not a participant in the project, or not Kontingentledning: skipped, since
    the app only reads the field for CMT members;
  * already holding this value: left alone;
  * holding a different, non-empty value: skipped unless --overwrite, since
    someone may already have set it by hand in Scoutnet.

Dry run by default; --write sends one PUT for everything that needs it. Keys
come from SCOUTNET_PROJECTS in src/.env, which must have an update_key.

    python3 tools/seed_cmt_roles_question.py              # dry run
    python3 tools/seed_cmt_roles_question.py --write
"""

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSV_FILE = REPO / "cmt-roles.csv"
ENV_FILE = REPO / "src" / ".env"

PROJECT_API = "https://www.scoutnet.se/api/project/get"
CHECKIN_API = "https://www.scoutnet.se/api/project/checkin"

QUESTION_CMT_ROLE = "120008"
SEPARATOR = " | "
# 90951 "Typ av anmälan" on form 47115; 64031 is Kontingentledning. Same map as
# APPLICATION_TYPE_MAP in src/app/scoutnet_forms.py.
QUESTION_APPLICATION_TYPE = "90951"
APPLICATION_TYPE_CMT = "64031"


def load_project() -> dict:
    """The single project in SCOUTNET_PROJECTS from src/.env."""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "SCOUTNET_PROJECTS":
            projects = json.loads(value.strip().strip("'\""))
            if len(projects) != 1:
                sys.exit(f"Expected one project in SCOUTNET_PROJECTS, found {len(projects)}")
            if not projects[0].get("update_key"):
                sys.exit("SCOUTNET_PROJECTS has no update_key")
            return projects[0]
    sys.exit(f"No SCOUTNET_PROJECTS in {ENV_FILE}")


def load_mapping(path: Path) -> dict[int, tuple[str, str]]:
    """member_no -> (Funktion, Roll), as labels, not slugs."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    mapping = {}
    for row in rows:
        member_no = (row.get("Medlemsnummer") or "").strip()
        funktion = (row.get("Funktion") or "").strip()
        roll = (row.get("Roll") or "").strip()
        if not member_no or not funktion:
            print(f"  skip CSV row without member number or Funktion: {row}")
            continue
        mapping[int(member_no)] = (funktion, roll)
    return mapping


def fetch_participants(project: dict) -> dict[int, dict]:
    query = urllib.parse.urlencode({"id": project["id"], "key": project["member_key"]})
    with urllib.request.urlopen(f"{PROJECT_API}/participants?{query}", timeout=60) as resp:
        data = json.load(resp)
    return {int(no): p for no, p in data["participants"].items()}


def write_answers(project: dict, answers: dict[int, str]) -> dict:
    body = {str(no): {"questions": {QUESTION_CMT_ROLE: {"value": value}}} for no, value in answers.items()}
    query = urllib.parse.urlencode({"id": project["id"], "key": project["update_key"]})
    request = urllib.request.Request(
        f"{CHECKIN_API}?{query}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        return json.load(resp)


def name(p: dict) -> str:
    return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", type=Path, default=CSV_FILE)
    parser.add_argument("--write", action="store_true", help="write to Scoutnet (default is a dry run)")
    parser.add_argument("--overwrite", action="store_true", help="replace values that differ from the CSV")
    args = parser.parse_args()

    project = load_project()
    mapping = load_mapping(args.csv)
    participants = fetch_participants(project)
    print(f"{len(mapping)} CSV rows, {len(participants)} Scoutnet participants\n")

    to_write: dict[int, str] = {}
    for member_no, (funktion, roll) in sorted(mapping.items()):
        value = f"{funktion}{SEPARATOR}{roll}" if roll else funktion
        p = participants.get(member_no)
        if p is None:
            print(f"  skip {member_no}: not a participant in the project")
            continue
        questions = p.get("questions") or {}
        who = f"{member_no} {name(p)}"
        if questions.get(QUESTION_APPLICATION_TYPE) != APPLICATION_TYPE_CMT:
            print(f"  skip {who}: not Kontingentledning in Scoutnet")
            continue
        current = (questions.get(QUESTION_CMT_ROLE) or "").strip()
        if current == value:
            print(f"  same {who}: {value!r}")
            continue
        if current and not args.overwrite:
            print(f"  KEEP {who}: Scoutnet has {current!r}, CSV says {value!r} (--overwrite to replace)")
            continue
        state = "" if p.get("confirmed") and not p.get("cancelled") else " [unconfirmed or cancelled]"
        print(f"  set  {who}: {current!r} -> {value!r}{state}")
        to_write[member_no] = value

    missing = [
        p
        for no, p in sorted(participants.items())
        if (p.get("questions") or {}).get(QUESTION_APPLICATION_TYPE) == APPLICATION_TYPE_CMT and no not in mapping
    ]
    for p in missing:
        print(f"  note {p['member_no']} {name(p)}: Kontingentledning in Scoutnet, not in the CSV")

    print(f"\n{len(to_write)} to write")
    if not to_write or not args.write:
        if to_write:
            print("Dry run; rerun with --write to send.")
        return

    write_answers(project, to_write)
    # Read back rather than trust the response: for a PUT covering many members,
    # its updated_questions listed only one of them (2026-10-14).
    participants = fetch_participants(project)
    wrong = [
        no
        for no, value in to_write.items()
        if ((participants.get(no) or {}).get("questions") or {}).get(QUESTION_CMT_ROLE, "").strip() != value
    ]
    print(f"Read back: {len(to_write) - len(wrong)} of {len(to_write)} hold the new value")
    for member_no in wrong:
        print(f"  NOT WRITTEN {member_no}")


if __name__ == "__main__":
    main()
