"""Take a next of kin's e-post from the Scoutnet profile when it looks like an update.

Scoutnet copies the form's Närstående answers into the member profile, but not
the other way, so an address corrected in the profile never reaches the form.
This puts the profile's Anhörig 1/2 e-post (contact types "33"/"34") in place of
the form's Närstående 1/2 e-post when the two look like the same person:

- the form e-post is blank but the Närstående has a name,
- the addresses differ by at most two characters (a typo fix),
- the local part is unchanged and only the domain is new, or
- the Närstående's surname or first name appears in the profile address.

A profile address already used anywhere in the form, or one with no apparent
link to the Närstående, is left alone. Rules as in tools/compare_next_of_kin.py.
"""

import re
import unicodedata

PROFILE_EMAIL = {"1": "33", "2": "34"}


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _same_person(name: str, form: str, profile: str) -> bool:
    if not form:
        return bool(name)
    if _distance(form, profile) <= 2 or form.split("@")[0] == profile.split("@")[0]:
        return True
    # Names of under three letters match too much by accident.
    local = _fold(profile.split("@")[0])
    return any(len(t) >= 3 and t in local for t in re.split(r"[\s-]+", _fold(name)))


def apply_profile_emails(contact_info: dict, profile_contacts) -> int:
    """Update contact_info in place; returns how many e-posts were replaced."""
    if not isinstance(profile_contacts, dict):
        return 0
    sections = {}
    for n in PROFILE_EMAIL:
        sections[n] = next(
            (s for s in contact_info.values() if f"nextOfKin{n}Email" in s or f"nextOfKin{n}Name" in s), None
        )
    form_emails = {str(s.get(f"nextOfKin{n}Email") or "").strip().lower() for n, s in sections.items() if s is not None}

    replaced = 0
    for n, key in PROFILE_EMAIL.items():
        section = sections[n]
        profile = str(profile_contacts.get(key) or "").strip()
        if section is None or not profile or profile.lower() in form_emails:
            continue
        form = str(section.get(f"nextOfKin{n}Email") or "").strip().lower()
        if not _same_person(str(section.get(f"nextOfKin{n}Name") or "").strip(), form, profile.lower()):
            continue
        section[f"nextOfKin{n}Email"] = profile
        replaced += 1
    return replaced
