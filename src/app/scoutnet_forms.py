import csv
import json
import logging
from collections import Counter
from pathlib import Path

from . import scoutnet_db
from .roles import roles_for_participant
from .scoutnet import CachedProject, ProjectCache, ScoutnetProjectData

logger = logging.getLogger(__name__)

# Hand-maintained, and the source of truth for forms_data: which questions
# appear, under which headings, in which order, and what each is called in the
# API. Change the output by editing the JSON, not this file.
# See docs/forms-data-decoder.md.
TEMPLATE_FILE = Path(__file__).parent / "forms_template.json"

# Hand-maintained, one-off export: member_no -> Postort/Land. Not Scoutnet
# data, so it is joined in here rather than decoded from p["questions"].
CITY_FILE = Path(__file__).parent / "members_city.csv"


# 84942: Typ av ansökan
# - 57999: IST (funktionär)
# - 58000: Deltagare
# 90951: Typ av anmälan
# - 62319: Avdelningsledare
# - 64031: Kontingentledning

# 110268: Accesstyp
# - 73658: Ingen
# - 73659: Intern information
# - 73868: Hälsa plus intern information
# - 73660: Avdelningsledare

# 107592: Avdelning (Ledare)
# 88168: Avdelning (Deltagare)


def _load_templates(qdefs: dict) -> dict:
    """Read forms_template.json and check its question ids against the forms.

    Each entry is keyed by the Scoutnet question id - the same id the answers
    arrive under - and holds {"key": <name the API and GUI use>, "label": <the
    exact wording that id was asked with>}.

    Logs rather than raises: a bad template must not take the API down.
    """
    templates = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
    labels_by_key: dict[str, tuple[str, str, str]] = {}
    for form_name, tabs in templates.items():
        for tab_title, sections in tabs.items():
            for section_title, questions in sections.items():
                for qid, entry in questions.items():
                    where = f"{form_name}/{tab_title}/{section_title}"
                    if qid not in qdefs:
                        logger.error(
                            "Template %s: question id %s is not defined by any form - it will never be filled in",
                            where,
                            qid,
                        )
                    elif not entry.get("key"):
                        logger.error("Template %s: question id %s has no key", where, qid)
                    else:
                        # One key means one question: ids may share a key only
                        # when they are asked with identical wording, otherwise
                        # the GUI's static label table cannot be right for both.
                        seen = labels_by_key.setdefault(entry["key"], (where, qid, entry.get("label")))
                        if seen[2] != entry.get("label"):
                            logger.error(
                                "Template %s: id %s and id %s (%s) share key %s but ask different questions - give them separate keys",
                                where,
                                qid,
                                seen[1],
                                seen[0],
                                entry["key"],
                            )
    return templates


def _load_member_cities(path: Path = CITY_FILE) -> dict[int, str]:
    """Read CITY_FILE into a member_no -> city string map.

    City is Postort; Postnummer is skipped for now, and Land is appended in
    parentheses when it isn't Sverige, since a bare city name is ambiguous
    without a country for most of the non-Swedish rows.

    Logs rather than raises: a missing or malformed file must not take the
    API down, it just means nobody gets a city.
    """
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        logger.warning("Could not read member cities from %s: %s", path, exc)
        return {}

    cities: dict[int, str] = {}
    for row in rows:
        raw_member_no = (row.get("Medlemsnummer") or "").strip()
        if not raw_member_no:
            continue
        try:
            member_no = int(raw_member_no)
        except ValueError:
            logger.warning("Member cities: %r is not a member number; skipping row", raw_member_no)
            continue
        city = (row.get("Postort") or "").strip()
        if not city:
            continue
        country = (row.get("Land") or "").strip()
        if country and country != "Sverige":
            city = f"{city} ({country})"
        cities[member_no] = city
    return cities


def _decode_answer(qdef: dict, raw, unmapped: Counter | None = None):
    """One stored answer as readable text, or None when it was left blank."""
    if raw == "" or raw is None:
        return None
    if isinstance(raw, list):
        if not raw:
            return None
        if qdef["type"] == "choice":
            options = [o for v in raw if (o := _decode_choice(qdef, v, unmapped)) is not None]
            return options or None
        return raw

    if qdef["type"] == "choice":
        return _decode_choice(qdef, raw, unmapped)

    # Guardian contact details arrive as a JSON-encoded string regardless of the
    # declared question type: {"linked_id": ..., "value": "..."}.
    if isinstance(raw, str) and raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "value" in parsed:
            return parsed["value"] or None

    return raw


def _decode_choice(qdef: dict, value_id, unmapped: Counter | None = None) -> str | None:
    """Map a stored choice id to its option text, or None if it means "unanswered".

    An id matching no option is Scoutnet's marker for a radio that was shown and
    left untouched - always the bare string "1". It is not an answer: rendering
    it raw, as ScoutView does, reads as "mildly allergic" on a scale whose
    lowest rung is labelled "1". Counted so the volume stays visible in the log.
    See docs/scoutnet-data-quirks.md for the evidence.
    """
    choice = qdef["choices"].get(str(value_id))
    if choice is None:
        if unmapped is not None:
            unmapped[f"{qdef['question'][:60]} = {value_id!r}"] += 1
        return None
    return choice["option"]


# Tabs that leave the template as contact_info instead of forms_data. The split
# is an access-level one, not a display one: forms_data is health and food
# information, dropped wholesale for callers without the role for it (see
# participants.py), while contact details are available with basic access. The
# template still describes these questions like any other, so they keep their
# keys, labels and order - only their destination differs.
CONTACT_TABS = {"Grundläggande information"}


def _split_contact(forms_data: dict) -> tuple[dict, dict]:
    """Separate the basic-access contact tabs out of the restricted answers.

    Returns (forms_data, contact_info). contact_info drops the form and tab
    levels - they carry no information once the health tabs are gone - and keeps
    the section grouping, so the GUI can render it with the same code as a
    forms_data section.
    """
    restricted: dict = {}
    contact: dict = {}
    for form_name, tabs in forms_data.items():
        keep = {tab: sections for tab, sections in tabs.items() if tab not in CONTACT_TABS}
        for tab, sections in tabs.items():
            if tab in CONTACT_TABS:
                for section_title, answers in sections.items():
                    contact.setdefault(section_title, {}).update(answers)
        if keep:
            restricted[form_name] = keep
    return restricted, contact


def _build_forms_data(raw_answers: dict, qdefs: dict, templates: dict, unmapped: Counter | None = None) -> dict:
    """Fill one member's answers into the templates, keyed form -> tab -> section.

    Every form is tried. The forms share no question ids, so one the member
    never filled in yields nothing and is left out - no member-type mapping
    needed, and a third form would work with no change here.
    """
    return {
        form_name: filled
        for form_name, template in templates.items()
        if (filled := _fill_template(raw_answers, qdefs, template, unmapped))
    }


def _fill_template(raw_answers: dict, qdefs: dict, template: dict, unmapped: Counter | None = None) -> dict:
    """One form's tab/section structure filled with this member's answers.

    Walks the template, not the answers, so the template alone decides what is
    shown and in what order. Unanswered questions are left out, and sections or
    tabs that end up empty are dropped rather than emitted bare.
    """
    forms_data: dict = {}
    for tab_title, sections in template.items():
        filled_sections = {}
        for section_title, questions in sections.items():
            answers = {}
            for qid, entry in questions.items():
                key = entry["key"]
                # Ids sharing a key ask the same question; first one holding an
                # answer wins, so a blank id cannot mask a filled one.
                if key in answers or qid not in raw_answers or qid not in qdefs:
                    continue
                value = _decode_answer(qdefs[qid], raw_answers[qid], unmapped)
                if value is not None:
                    answers[key] = value
            if answers:
                filled_sections[section_title] = answers
        if filled_sections:
            forms_data[tab_title] = filled_sections
    return forms_data


def scoutnet_forms_decoder(
    all_project_data: list[ScoutnetProjectData],
    cache: ProjectCache,
) -> None:

    application_type_map = {
        "58000": "Deltagare",
        "57999": "IST",
        "62319": "Avdelningsledare",
        "64031": "Kontingentledning",
    }
    access_map = {
        "73658": "Ingen",
        "73659": "Intern information",
        "73868": "Hälsa plus intern information",
        "73660": "Avdelningsledare",
    }
    # Which question holds the travel package, per applicant type: Deltagandetyp,
    # Funktionärstyp and "Med rundresa eller direktresa" all ask the same thing
    # of different people. Read the one belonging to the member's own type - a
    # member who changed type mid-application leaves a stale answer behind on
    # the question they abandoned, and one member in the data has exactly that.
    participation_question_map = {
        "Deltagare": "84941",
        "IST": "85095",
        "Avdelningsledare": "93357",
        "Kontingentledning": "93357",
    }
    # Normalised, because this block is for filtering and the three questions
    # word their options differently ("Deltagare med rundresa" vs "Med rundresa
    # (under 26 år)" vs "Med rundresa"). The IST option's "under 26 år" is a
    # condition on who may pick it, not part of the answer; age is in `born`.
    participation_type_map = {
        "57995": "Rundresa",  # Deltagare med rundresa
        "57996": "Direktresa",  # Deltagare med direktresa
        "58084": "Rundresa",  # IST, med rundresa (under 26 år)
        "58082": "Egen resa",  # IST, med egen resa
        "64032": "Rundresa",  # Ledare, med rundresa
        "64033": "Direktresa",  # Ledare, med direktresa
    }

    participants = {}

    project = all_project_data[0]  # We only have one project here. So we split into groups based on response
    pdata = project.participants["participants"]
    labels = project.participants["labels"]
    qdefs = project.questions["questions"]  # qid -> definition, both forms merged
    templates = _load_templates(qdefs)
    member_cities = _load_member_cities()
    unmapped: Counter = Counter()  # choice answers that match no option, summarised below
    logger.debug("Processing %s participants for project %s", len(pdata), project.project_name)

    for p in pdata.values():
        if not p["confirmed"] or p["cancelled"]:
            continue  # Only handle confirmed participants

        application_type = p["questions"].get("84942") or p["questions"].get("90951") or ""
        if not application_type:
            logger.error("No application type found for member %s", p["member_no"])
        if not (member_type := application_type_map.get(application_type, "")):
            logger.error("Application type %s not found in map", application_type)
        troop = p["questions"].get("107592") or p["questions"].get("88168") or ""

        # Left empty when unanswered, which is normal: Kontingentledning are not
        # asked for a travel package at all (all 59 of them in the 2026-09 data).
        participation_answer = p["questions"].get(participation_question_map.get(member_type, ""))
        participation_type = participation_type_map.get(participation_answer, "") if participation_answer else ""
        if participation_answer and not participation_type:
            logger.error("Participation type %s not found in map (member %s)", participation_answer, p["member_no"])

        forms_data, contact_info = _split_contact(_build_forms_data(p["questions"], qdefs, templates, unmapped))
        # `or`, not a .get() default: Scoutnet stores this key with a null
        # value when unset, and a default only covers a key that is absent.
        access_type = p["questions"].get("110268") or "73658"
        if not (access_level := access_map.get(access_type, "")):
            logger.error("Access type %s not found in map (member %s)", access_type, p["member_no"])

        # Key and field are both int. Scoutnet is not consistent about whether
        # member_no arrives as a number or a string, and the disk cache turns
        # every key into a string on the way through JSON, so the type is pinned
        # here — the one place participants is built — rather than guessed at
        # each lookup. See _load_cache_from_disk() for the other half.
        member_no = int(p["member_no"])

        # access_level is only ever an input to the wsj27:access:<level> role,
        # never read on its own - mint the role here, once, rather than
        # storing access_level for roles.py to turn into one on every request.
        roles = roles_for_participant(
            {"member_type": member_type, "troop": troop, "member_no": member_no, "access_level": access_level}
        )

        partdata = {  # Save som basic data that is quick to filter on
            "name": f"{p['first_name']} {p['last_name']}",
            "member_no": member_no,
            "born": p["date_of_birth"],
            "sex": labels["sex"].get(p["sex"], ""),
            "member_group": cache.group_map.get(
                p["primary_membership_info"]["group_id"] if p["primary_membership_info"] else 0, ""
            ),
            "email": p["primary_email"],
            "mobile": p["contact_info"].get("1") if p["contact_info"] else None,
            "city": member_cities.get(member_no),
            "member_type": member_type,
            "participation_type": participation_type,
            "roles": roles,
            "troop": troop,
            # Basic access: contact details, kept out of the health-gated block.
            "contact_info": contact_info,
            "forms_data": forms_data,
        }
        # This app's own stored values (patrol, avatar URL, assigned roles), as
        # they are - consumers read them from here, and /roles merges the roles.
        partdata[scoutnet_db.FIELD] = scoutnet_db.stored_for(p["questions"], member_no, member_type)
        participants[member_no] = partdata

    if unmapped:
        logger.warning(
            "Ignored %s untouched-radio markers over %s questions; most common: %s",
            sum(unmapped.values()),
            len(unmapped),
            "; ".join(f"{q} ({n})" for q, n in unmapped.most_common(3)),
        )

    cache.projects = {
        project.project_id: CachedProject(
            project_id=project.project_id,
            project_name=project.project_name,
            participants=participants,
            questions=project.questions,
            groups=None,
        )
    }
