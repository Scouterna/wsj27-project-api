"""
Standalone exploration script: decode raw form answers in
src/.dev_cache/92fa15301e2df7c8.json using the two form definitions
(76e8cacc3235d4c0.json = Avdelningsledare/Kontingentledning,
ea1c00abf6a5c846.json = Deltagare/IST) also cached in src/.dev_cache.

Not wired into the app yet - just to inspect the decoded shape before
deciding where this logic should live.
"""

import json
import re
from pathlib import Path

from question_keys import (
    EXCLUDED_TABS,
    INTERNAL_KEYS,
    NON_ANSWER_KEYS,
    QUESTION_KEYS,
    SCOUTNET_MIRROR_KEYS,
)

REPO = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO / "src" / ".dev_cache"

FORM_FILES = {
    "avdelningsledare_kontingentledning": "76e8cacc3235d4c0.json",
    "deltagare_ist": "ea1c00abf6a5c846.json",
}
PARTICIPANTS_FILE = "92fa15301e2df7c8.json"


def load_form(filename: str) -> dict:
    raw = json.loads((CACHE_DIR / filename).read_text())
    return {
        "meta": raw["form"],
        "qdefs": raw["questions"],  # qid -> {question, type, choices, tab_id, section_id, ...}
        "tabs": raw["tabs"],
        "sections": raw["sections"],
        "qids": set(raw["questions"].keys()),
    }


def classify_form(question_ids: set[str], forms: dict[str, dict]) -> str | None:
    """Pick the form whose question-id set overlaps most with this participant's answers."""
    best_name, best_count = None, 0
    for name, form in forms.items():
        overlap = len(question_ids & form["qids"])
        if overlap > best_count:
            best_name, best_count = name, overlap
    return best_name


def decode_choice_value(qdef: dict, value_id: str) -> str | None:
    """None when the id matches no option: an untouched radio - see the app's _decode_choice()."""
    choice = qdef["choices"].get(str(value_id))
    return choice["option"] if choice else None


def decode_answer(qdef: dict, raw):
    """Turn a raw stored answer into a human-readable value, or None if unanswered."""
    if raw == "" or raw is None:
        return None
    if isinstance(raw, list):
        if not raw:
            return None
        if qdef["type"] == "choice":
            options = [o for v in raw if (o := decode_choice_value(qdef, v)) is not None]
            return options or None
        return raw

    if qdef["type"] == "choice":
        return decode_choice_value(qdef, raw)

    # Some fields (e.g. linked guardian contact details) are stored as a
    # JSON-encoded string {"linked_id": ..., "value": "..."} regardless of
    # the declared question type ("text" or "other_unsupported_by_api").
    if isinstance(raw, str) and raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "value" in parsed:
            return parsed["value"] or None

    return raw


def group_by_tab_section(items, form: dict, value_fn) -> list[dict]:
    """Shared tabs -> sections -> {short key: value} grouping, in form order.

    `items` is an iterable of (qid, qdef) pairs already filtered/ordered by
    the caller; `value_fn(qid, qdef)` produces the value stored under that
    question's short key in the matching section's "questions" dict.
    """
    tabs: dict[str, dict] = {}
    tab_order: list[str] = []

    for qid, qdef in items:
        tab_id = str(qdef["tab_id"])
        section_id = str(qdef["section_id"])
        if tab_id not in tabs:
            tabs[tab_id] = {
                "title": form["tabs"].get(tab_id, {}).get("title") or None,
                "sections": {},
                "section_order": [],
            }
            tab_order.append(tab_id)
        tab = tabs[tab_id]
        if section_id not in tab["sections"]:
            tab["sections"][section_id] = {
                "title": form["sections"].get(section_id, {}).get("title") or None,
                "questions": {},
            }
            tab["section_order"].append(section_id)
        tab["sections"][section_id]["questions"][QUESTION_KEYS[qid]] = value_fn(qid, qdef)

    return [
        {
            "title": tabs[tid]["title"],
            "sections": [
                section for sid in tabs[tid]["section_order"] if (section := tabs[tid]["sections"][sid])["questions"]
            ],
        }
        for tid in tab_order
    ]


def collect_flat(raw_answers: dict, form: dict, keys: set[str]) -> dict:
    """Flat {short key: answer} for one of the non-answer buckets."""
    out = {}
    for qid, qdef in form["qdefs"].items():
        key = QUESTION_KEYS[qid]
        if key not in keys or qid not in raw_answers:
            continue
        value = decode_answer(qdef, raw_answers[qid])
        if value is not None:
            out[key] = value
    return out


def report_dropped(forms: dict[str, dict]) -> None:
    """List the questions the template leaves out, so the loss stays visible."""
    for name, form in forms.items():
        for tab in build_form_reference(form):
            for section in tab["sections"]:
                if tab["title"] in EXCLUDED_TABS:
                    why = "excluded tab"
                elif tab["title"] is None or section["title"] is None:
                    why = "untitled"
                else:
                    continue
                where = f"tab={tab['title']!r} section={section['title']!r}"
                print(f"  dropped ({why}: {where}) in {name}: {', '.join(section['questions'])}")


def to_title_dict(tabs: list[dict]) -> dict:
    """Turn the tab/section lists into {tab title: {section title: {key: value}}}.

    Tabs and sections whose title is empty in Scoutnet are skipped, along with
    everything under them, as are the tabs in EXCLUDED_TABS - see report_dropped()
    for what that costs.
    """
    out: dict[str, dict] = {}
    for tab in tabs:
        if tab["title"] is None or tab["title"] in EXCLUDED_TABS:
            continue
        sections = {
            section["title"]: section["questions"] for section in tab["sections"] if section["title"] is not None
        }
        # A duplicate title would silently swallow the earlier section's
        # questions; the forms are closed, so this should never fire.
        titles = [s["title"] for s in tab["sections"] if s["title"] is not None]
        assert len(titles) == len(set(titles)), f"duplicate section title in tab {tab['title']!r}"
        if sections:
            out[tab["title"]] = sections
    return out


def build_form_reference(form: dict) -> list[dict]:
    """tabs -> sections -> {short key: question text}, still in list form.

    Intermediate step towards the template: excludes the non-answer keys, keeps
    the untitled tabs/sections that to_title_dict() then drops, so that
    report_dropped() can still see and report them.
    """
    items = [(qid, qdef) for qid, qdef in form["qdefs"].items() if QUESTION_KEYS[qid] not in NON_ANSWER_KEYS]
    return group_by_tab_section(items, form, lambda qid, qdef: qdef["question"])


TEMPLATE_FILE = REPO / "src" / "app" / "forms_template.json"


def load_templates() -> dict:
    """The hand-maintained {form: {tab: {section: {key: question text}}}} template.

    This file is the source of truth for forms_data: it fixes which questions
    are included, under which headings, and in which order. Swap each question
    text for the participant's answer and you have their forms_data. Edit the
    file to change the output - nothing here regenerates it.
    """
    return json.loads(TEMPLATE_FILE.read_text())


def build_template(form: dict) -> dict:
    """Generate a template from a form definition - only for bootstrapping a new one."""
    return to_title_dict(build_form_reference(form))


def validate_templates(templates: dict, forms: dict[str, dict]) -> None:
    """Check a hand-edited template against the form definitions.

    Catches the three ways a hand edit goes wrong: a key that doesn't exist, a
    key belonging to the other form, and a question quietly left out.
    """
    problems, notes = [], []
    for form_name, tabs in templates.items():
        if form_name not in forms:
            problems.append(f"template has unknown form {form_name!r}")
            continue
        form = forms[form_name]
        form_keys = {QUESTION_KEYS[qid] for qid in form["qdefs"]}
        all_keys = set(QUESTION_KEYS.values())

        seen: dict[str, str] = {}  # key -> the section that first used it
        seen_ids: set[str] = set()
        for tab_title, sections in tabs.items():
            for section_title, questions in sections.items():
                where = f"{form_name} / {tab_title} / {section_title}"
                for qid, entry in questions.items():
                    if qid in seen_ids:
                        problems.append(f"{where}: question id {qid} is listed twice")
                    seen_ids.add(qid)
                    if not isinstance(entry, dict) or not entry.get("key"):
                        problems.append(f"{where}: id {qid} has no key")
                        continue
                    key, text = entry["key"], entry.get("label")
                    # The same key twice is fine (one id per wording), but only
                    # within one section - otherwise the answer's home is random.
                    if seen.setdefault(key, section_title) != section_title:
                        problems.append(f"{where}: key {key!r} is also used in section {seen[key]!r}")
                    if key not in all_keys:
                        problems.append(f"{where}: unknown key {key!r}")
                    if qid not in form["qdefs"]:
                        problems.append(f"{where}: id {qid} is not asked in this form")
                    elif QUESTION_KEYS.get(qid) != key:
                        problems.append(f"{where}: id {qid} belongs to key {QUESTION_KEYS.get(qid)!r}, not {key!r}")
                    if not isinstance(text, str) or not text.strip():
                        problems.append(f"{where}: id {qid} has no label")

        omitted = sorted(form_keys - set(seen) - NON_ANSWER_KEYS)
        if omitted:
            notes.append(f"  {form_name}: not in template ({len(omitted)}): {', '.join(omitted)}")

    if notes:
        print("Questions the template leaves out (fine if deliberate):")
        print("\n".join(notes))
    if problems:
        raise SystemExit("Template problems:\n  " + "\n  ".join(problems))


def build_forms_data(raw_answers: dict, forms: dict[str, dict], templates: dict) -> dict:
    """Fill the templates, keyed form -> tab -> section -> {key: answer}.

    Every form is tried; the forms share no question ids, so one the member
    never filled in yields nothing and is left out.
    """
    return {
        form_name: filled
        for form_name, template in templates.items()
        if (filled := fill_template(raw_answers, forms[form_name], template))
    }


def fill_template(raw_answers: dict, form: dict, template: dict) -> dict:
    """Fill the template with one participant's answers.

    Walks the template rather than the raw answers, so the output shape is the
    template's. Questions the participant didn't answer are left out, and any
    section or tab that ends up empty is dropped rather than emitted bare.
    """
    forms_data: dict[str, dict] = {}
    for tab_title, sections in template.items():
        filled_sections = {}
        for section_title, questions in sections.items():
            answers = {}
            # Keyed by question id - the same id the answers arrive under. Two
            # ids can share a key; the first holding an answer wins, so a blank
            # id can't mask a filled one.
            for qid, entry in questions.items():
                key = entry["key"]
                if key in answers or qid not in raw_answers or qid not in form["qdefs"]:
                    continue  # already answered, or belongs to the other form
                value = decode_answer(form["qdefs"][qid], raw_answers[qid])
                if value is not None:
                    answers[key] = value
            if answers:
                filled_sections[section_title] = answers
        if filled_sections:
            forms_data[tab_title] = filled_sections
    return forms_data


def decode_participant(participant: dict, forms: dict[str, dict], templates: dict[str, dict]) -> dict:
    raw_answers = participant.get("questions")
    question_ids = set(raw_answers.keys()) if isinstance(raw_answers, dict) else set()
    form_name = classify_form(question_ids, forms)

    forms_data: dict = {}
    contact: dict = {}
    internal: dict = {}
    if form_name and isinstance(raw_answers, dict):
        form = forms[form_name]
        forms_data = build_forms_data(raw_answers, forms, templates)
        # Kept out of forms_data but still decoded, so the API can feed them into
        # the general participant structure / internal handling instead.
        contact = collect_flat(raw_answers, form, SCOUTNET_MIRROR_KEYS)
        internal = collect_flat(raw_answers, form, INTERNAL_KEYS)

    return {
        "member_no": participant.get("member_no"),
        "first_name": participant.get("first_name"),
        "last_name": participant.get("last_name"),
        "primary_email": participant.get("primary_email"),
        "form": forms[form_name]["meta"]["title"] if form_name else None,
        "forms_data": forms_data,
        "contact": contact,
        "internal": internal,
    }


def build_key_labels(templates: dict[str, dict]) -> dict[str, str]:
    """Flat {key: label} for the GUI, taken straight from forms_template.json.

    The template is the source of truth for both the key and its wording, so a
    label fixed there reaches the GUI without anything else needing an edit.
    Only keys that can appear in forms_data are included - the rest never leave
    the API. validate_templates() has already checked that ids sharing a key
    also share a label, so the last writer here can never disagree.
    """
    labels: dict[str, str] = {}
    for tabs in templates.values():
        for sections in tabs.values():
            for questions in sections.values():
                for entry in questions.values():
                    labels[entry["key"]] = entry["label"]
    return dict(sorted(labels.items()))


def render_typescript(labels: dict[str, str]) -> str:
    """Emit the label table as a typed TS module (generated - never hand-edit)."""
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) for key in labels), "key is not a valid TS identifier"

    def union(keys) -> str:
        return "\n".join(f"  | {json.dumps(key)}" for key in sorted(keys))

    entries = "\n".join(f"  {key}: {json.dumps(text, ensure_ascii=False)}," for key, text in labels.items())
    return f"""// Generated by decode_participants.py - do not edit by hand.
// Short keys for the WSJ27 registration form questions. The forms are closed,
// so these keys are stable.

/** Every question that can appear in forms_data; generated from forms_template.json. */
export type QuestionKey =
{union(labels)};

/** Original (Swedish) question text, for displaying an answer with its question. */
export const QUESTION_LABELS: Record<QuestionKey, string> = {{
{entries}
}};

/** One answer: a single value, or several for multi-choice questions. */
export type Answer = string | string[];

/** Section heading -> the answers given under it. Unanswered questions are absent. */
export type FormsDataSection = Partial<Record<QuestionKey, Answer>>;

/** Tab heading -> its sections. Empty sections and tabs are omitted entirely. */
export type FormTabs = Record<string, Record<string, FormsDataSection>>;

/** The forms a member filled in, by form name ("deltagare_ist",
 *  "avdelningsledare_kontingentledning"). A member normally has exactly one,
 *  but the shape allows several - and none, if they answered nothing. */
export type FormsData = Record<string, FormTabs>;

/** Contact details, by section heading. Available with basic access, unlike
 *  forms_data, which is dropped for callers without the health-data role. */
export type ContactInfo = Record<string, FormsDataSection>;

export type MemberType =
  | "Deltagare"
  | "IST"
  | "Avdelningsledare"
  | "Kontingentledning";

/** A participant row as the API returns it. */
export interface Participant {{
  name: string;
  member_no: number;
  /** Date of birth, "YYYY-MM-DD". */
  born: string;
  sex: string;
  member_group: string;
  email: string | null;
  mobile: string | null;
  member_type: MemberType;
  access_level: string;
  troop: string;
  /** Next of kin and the contact details the member confirmed in the form.
   *  Note these are a snapshot from application time; `email` and `mobile`
   *  above are the live profile values and can differ. */
  contact_info: ContactInfo;
  /** Health and food answers, keyed by form, tab then section title. Omitted
   *  entirely when the caller's access level does not cover them - always
   *  check before reading. */
  forms_data?: FormsData;
}}
"""


def check_key_coverage(forms: dict[str, dict]) -> None:
    """Every question must have a short key; fail loudly if the table drifts."""
    missing = [
        (name, qid, qdef["question"])
        for name, form in forms.items()
        for qid, qdef in form["qdefs"].items()
        if qid not in QUESTION_KEYS
    ]
    if missing:
        raise SystemExit(f"questions without a short key: {missing}")


def check_key_collisions(participants: dict, forms: dict[str, dict]) -> None:
    """Two questions sharing a key must never both be answered by one person."""
    qdefs = {qid: qdef for form in forms.values() for qid, qdef in form["qdefs"].items()}
    collisions = []
    for pid, participant in participants.items():
        raw = participant.get("questions")
        if not isinstance(raw, dict):
            continue
        seen: dict[str, str] = {}
        for qid, value in raw.items():
            qdef = qdefs.get(qid)
            if qdef is None or decode_answer(qdef, value) is None:
                continue
            key = QUESTION_KEYS[qid]
            if key in seen:
                collisions.append((pid, key, seen[key], qid))
            seen[key] = qid
    print(f"Key collisions (same key answered twice by one person): {len(collisions)}")
    for c in collisions[:10]:
        print("   ", c)


def main():
    forms = {name: load_form(filename) for name, filename in FORM_FILES.items()}
    participants = json.loads((CACHE_DIR / PARTICIPANTS_FILE).read_text())["participants"]
    check_key_coverage(forms)
    check_key_collisions(participants, forms)

    templates = load_templates()
    validate_templates(templates, forms)
    decoded = {pid: decode_participant(p, forms, templates) for pid, p in participants.items()}

    counts = {}
    for d in decoded.values():
        counts[d["form"]] = counts.get(d["form"], 0) + 1
    print("Participants by form:", counts)

    # Print a couple of full examples, one per form, for a quick sanity check.
    shown = set()
    for pid, d in decoded.items():
        if d["form"] and d["form"] not in shown:
            shown.add(d["form"])
            print(f"\n=== {pid} ({d['form']}) ===")
            print(json.dumps(d, indent=2, ensure_ascii=False))
        if len(shown) == len(FORM_FILES):
            break

    scratchpad = Path(__file__).resolve().parent / "generated"
    scratchpad.mkdir(parents=True, exist_ok=True)

    out_path = scratchpad / "decoded_participants.json"
    out_path.write_text(json.dumps(decoded, indent=2, ensure_ascii=False))
    print(f"\nFull decoded output ({len(decoded)} participants) written to {out_path}")

    print(f"Template loaded from {TEMPLATE_FILE}")

    # Flat key -> question text table for the GUI to embed. A key used by both
    # forms keeps every wording variant so nothing is silently lost.
    flat: dict[str, dict] = {}
    for name, form in forms.items():
        for qid, qdef in form["qdefs"].items():
            entry = flat.setdefault(
                QUESTION_KEYS[qid],
                {
                    "question": qdef["question"],
                    "tab": form["tabs"].get(str(qdef["tab_id"]), {}).get("title") or None,
                    "section": form["sections"].get(str(qdef["section_id"]), {}).get("title") or None,
                    "forms": [],
                    "question_ids": [],
                },
            )
            entry["forms"].append(name)
            entry["question_ids"].append(qid)
            if qdef["question"] != entry["question"]:
                entry.setdefault("wording_variants", []).append(qdef["question"])
    map_path = scratchpad / "question_key_map.json"
    map_path.write_text(json.dumps(flat, indent=2, ensure_ascii=False))
    print(f"Flat key -> question map ({len(flat)} keys) written to {map_path}")

    labels = build_key_labels(templates)
    labels_path = scratchpad / "question_labels.json"
    labels_path.write_text(json.dumps(labels, indent=2, ensure_ascii=False))
    print(f"Key -> question text labels ({len(labels)} keys) written to {labels_path}")

    ts_path = scratchpad / "questionKeys.ts"
    ts_path.write_text(render_typescript(labels))
    print(f"TypeScript const + QuestionKey union written to {ts_path}")


if __name__ == "__main__":
    main()
