"""Minting, assigning and serving WSJ27 roles.

This is the single definition of what a WSJ27 role *is*. The strings minted here
are what wsj27-auth-api puts into the tokens it signs, and what every consumer
mirrors. The format used to be defined in auth-api while this service matched it
with hardcoded literals, so a change to the namespace there would silently have
changed who could read health data here. Defining it here also keeps auth-api
project-agnostic: it caches and serves whatever role map it is given, and knows
nothing about troops or member types.

A participant's roles come from two sources, both handled here:

  * **Minted** from their Scoutnet form answers by roles_for_participant(),
    which scoutnet_forms.py calls once per member at decode time.
  * **Assigned** by hand and kept in scoutnet_db.py's stored object, under the
    `wsj27:access:` namespace that nothing else mints. POST /{member_id}/role
    below writes them.

The participant record's `roles` holds only the minted ones. The /roles endpoint
merges the assigned ones in as it serves, so an assignment reaches it at once.

Role *checking* (has_role/has_any_role/role_suffixes) lives with AuthUser in
authenctication.py, so that the rules for minting a role can change here without
anything else needing to be touched. Those checks are format-only — segment
comparison, no role names — so nothing there needs to track this file. The
actual role literals a caller depends on (e.g. HEALTH_ROLES in participants.py)
are pinned against what this module mints by tests/test_participant_access.py.

"""

import csv
import hashlib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from . import scoutnet_db
from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .scoutnet import get_single_project

settings = get_settings()
logger = logging.getLogger(__name__)

# --- Role model ---------------------------------------------------------------
#
# Roles have two to four colon-separated parts, e.g. "wsj27:cmt:admin:fa". A
# consumer can grant on the whole namespace ("wsj27:*"), on a prefix
# ("wsj27:cmt:*"), or require one exact role. The first part is always the wsj27
# namespace, and every segment after it is slugified (see _slug) so a segment
# can never contain a colon and pose as a deeper role.
ROLE_NAMESPACE = "wsj27"
ROLE_LEADER = "al"
ROLE_CMT = "cmt"
# A personal grant assigned by hand through scoutnet_db, e.g.
# "wsj27:access:Hälsa plus intern information". See "Assigned roles" below.
ROLE_ACCESS = "access"
# The same kind of grant, minted from the Scoutnet form's own access question.
# Being replaced by ROLE_ACCESS and kept out of its namespace meanwhile, so the
# two can never be mistaken for each other; removed once the grants have moved.
ROLE_LEGACY_ACCESS = "legacy-access"

# Only these member types get roles at all.
MEMBER_TYPE_LEADER = "Avdelningsledare"
MEMBER_TYPE_CMT = "Kontingentledning"

# access_level values that mean "no access role", alongside a blank value.
NO_ACCESS_LEVELS = {"ingen", ""}


# --- CMT detail roles -----------------------------------------------------------
#
# CMT members are further split by Funktion (Admin, Program, Support, ...) and
# Roll (FA, Medlem, Avdelningssupport, ...), giving "wsj27:cmt:<funktion>:<roll>".
# That split is not in Scoutnet, so it comes from a CSV mounted at
# CMT_ROLES_FILE; see load_cmt_roles().
#
# A Roll can carry a trailing "PL" (Platsledare - the coordinator within that
# Roll, e.g. "Hälsa PL"): _drop_pl_suffix() merges it into the plain Roll, so
# "Hälsa PL" and "Hälsa" mint the same role and get identical access. PL is a
# coordination title, not (yet) its own authorization scope - if that changes,
# it belongs as a fifth path segment (wsj27:cmt:<funktion>:<roll>:<pl>), not
# folded away here.
#
# member_no -> (funktion, roll), both already slugified. Populated by
# load_cmt_roles() at startup; empty until then, and empty is a valid state.
# Keyed by int to match CachedProject.participants.
_cmt_details: dict[int, tuple[str, str]] = {}

CSV_MEMBER_NO = "Medlemsnummer"
CSV_FUNKTION = "Funktion"
CSV_ROLL = "Roll"

# Matches only a trailing, whitespace-separated "PL" - "Hälsa PL" and
# "IST-support PL", not "PL Food house", where PL is part of the title itself
# rather than a suffix on it.
_ROLL_PL_SUFFIX = re.compile(r"\s+PL\s*$", re.IGNORECASE)


def _drop_pl_suffix(roll: str) -> str:
    """Strip a trailing "PL" qualifier so it mints the same role as the plain Roll."""
    return _ROLL_PL_SUFFIX.sub("", roll)


def _slug(value: str) -> str:
    """Lowercase a Swedish role label into a colon-safe role segment.

    "Avdelningssupport" -> "avdelningssupport", "FA/CET" -> "fa-cet". Accents
    are folded (å/ä/ö -> a/a/o) so that consumers which cannot carry non-ASCII
    role names — Discord server roles, for one — get the same string everyone
    else does. Any run of non-alphanumerics becomes a single dash, which also
    guarantees no segment can smuggle in a colon and fake a deeper role than it
    has.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def load_cmt_roles(path: Path | None = None) -> int:
    """Read the CMT Funktion/Roll CSV into memory. Returns the number of rows kept.

    Called once at startup. Rows without a member number are skipped: the CSV is
    also a working document for humans, and people who have not yet been matched
    to a Scoutnet account appear in it with the number column blank. They are the
    same rows that have a blank Roll, so there is nothing to grant them anyway.

    Every failure here is logged and swallowed rather than raised. A missing or
    malformed file must not take the service down — it degrades to plain
    "wsj27:cmt" for everyone, which is exactly what was served before this file
    existed.
    """
    path = path or settings.CMT_ROLES_FILE
    if path is None:
        logger.info("CMT_ROLES_FILE not set; CMT members get the plain %s:%s role", ROLE_NAMESPACE, ROLE_CMT)
        _cmt_details.clear()
        return 0

    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        logger.warning("Could not read CMT roles from %s: %s", path, exc)
        return len(_cmt_details)

    details: dict[int, tuple[str, str]] = {}
    for row in rows:
        raw_member_no = (row.get(CSV_MEMBER_NO) or "").strip()
        if not raw_member_no:
            continue  # Not yet matched to a Scoutnet member; nothing to key on.
        try:
            member_no = int(raw_member_no)
        except ValueError:
            # Hand-maintained file; a typo in the number column should cost that
            # one person their detail role, not everyone else theirs.
            logger.warning("CMT roles: %r is not a member number; skipping row", raw_member_no)
            continue
        funktion = _slug(row.get(CSV_FUNKTION) or "")
        roll = _slug(_drop_pl_suffix(row.get(CSV_ROLL) or ""))
        if not funktion:
            logger.warning("CMT roles: member %s has no %s; skipping", member_no, CSV_FUNKTION)
            continue
        details[member_no] = (funktion, roll)

    _cmt_details.clear()
    _cmt_details.update(details)
    logger.info("Loaded CMT Funktion/Roll for %d members from %s", len(details), path)
    return len(details)


def _cmt_role(member_no: int) -> str:
    """The CMT role for one member, as specific as the CSV allows.

    Falls back through "wsj27:cmt:<funktion>" to a plain "wsj27:cmt" rather than
    granting nothing, so a member missing from the CSV keeps the access they had
    before the file was introduced.
    """
    base = f"{ROLE_NAMESPACE}:{ROLE_CMT}"
    funktion, roll = _cmt_details.get(member_no, ("", ""))
    if not funktion:
        return base
    return f"{base}:{funktion}:{roll}" if roll else f"{base}:{funktion}"


def roles_for_participant(info: dict[str, Any]) -> list[str]:
    """Map one participant's project fields to WSJ27 roles.

    The rules, as of 2026-08-16:

      * Only `Avdelningsledare` and `Kontingentledning` get roles at all.
        Everyone else is a registered participant with no permissions.
      * `Avdelningsledare` gets `wsj27:al:<troop>` — the troop is part of
        the role because a leader's authority is scoped to their own troop.
      * `Kontingentledning` gets `wsj27:cmt:<funktion>:<roll>`, e.g.
        `wsj27:cmt:support:avdelningssupport`. Funktion and Roll come from the
        CSV at CMT_ROLES_FILE, not from Scoutnet; a member missing from it
        falls back to plain `wsj27:cmt`. A trailing "PL" on Roll is dropped
        (see _drop_pl_suffix), so "Hälsa PL" and "Hälsa" mint the same role.
      * `access_level` becomes `wsj27:legacy-access:<level>` unless it is
        "Ingen" or blank, so the absence of access is expressed by the absence
        of a role rather than by a role meaning "nothing". Named "legacy-"
        because the plain `wsj27:access:` namespace now belongs to hand-assigned
        grants, which are replacing this question; see merge_stored_roles().
        This one goes away once the grants have moved.

    Kept as a pure function of one participant record: it is the piece most
    likely to change, and this way it can be reasoned about and tested without
    the project data or the network.
    """
    member_type = str(info.get("member_type") or "").strip()
    roles: list[str] = []

    if member_type == MEMBER_TYPE_LEADER:
        troop = str(info.get("troop") or "").strip()
        if troop:
            roles.append(f"{ROLE_NAMESPACE}:{ROLE_LEADER}:{troop}")
        else:
            # A leader with no troop cannot be granted troop-scoped authority;
            # say so, because it is a data problem rather than a normal state.
            logger.warning("Participant is %s but has no troop; granting no leader role", MEMBER_TYPE_LEADER)
    elif member_type == MEMBER_TYPE_CMT:
        roles.append(_cmt_role(info.get("member_no")))
    else:
        # Not a role-bearing member type: no roles, and no access role either.
        return []

    access_level = str(info.get("access_level") or "").strip()
    if access_level.lower() not in NO_ACCESS_LEVELS:
        roles.append(f"{ROLE_NAMESPACE}:{ROLE_LEGACY_ACCESS}:{access_level}")

    return roles


# --- Assigned roles -------------------------------------------------------------
#
# Roles assigned by hand live in the participant's scoutnet_db object, as a list
# under STORED_ROLES_KEY. They are confined to `wsj27:access:`, which nothing
# else mints, so an assigned role can never pose as a troop or CMT role and it is
# always clear where a given role came from.

STORED_ROLES_KEY = "roles"
ASSIGNED_ROLE_PREFIX = (ROLE_NAMESPACE, ROLE_ACCESS)


def is_assigned_role(role: str) -> bool:
    """True if `role` is one that may be assigned by hand, matched segment-wise.

    Never `str.startswith`: "wsj27:accessx:..." is an unrelated role that a
    string prefix would accept on the way in and strip out on the way back.
    """
    return tuple(role.split(":")[: len(ASSIGNED_ROLE_PREFIX)]) == ASSIGNED_ROLE_PREFIX


def merge_stored_roles(roles: list[str], stored: Any, member_no: Any = None) -> list[str]:
    """`roles` plus the assigned roles in `stored`, sorted.

    `stored` is the raw value from the scoutnet_db object, so it is checked
    rather than trusted: anything malformed is logged and ignored. Sorted because
    /roles hashes its body for the ETag, and an unstable order would change the
    tag on every request.
    """
    if stored is None:
        stored = []
    elif not isinstance(stored, list):
        logger.error("Stored %s for member %s is not a list, ignoring", STORED_ROLES_KEY, member_no)
        stored = []

    merged = list(roles)
    for role in stored:
        if isinstance(role, str) and is_assigned_role(role):
            merged.append(role)
        else:
            logger.error("Ignoring stored role %r for member %s: not under wsj27:access:", role, member_no)
    return sorted(set(merged))


def _all_roles(member_id: int, info: dict) -> list[str]:
    """One participant's minted roles plus their hand-assigned ones."""
    stored = (info.get(scoutnet_db.FIELD) or {}).get(STORED_ROLES_KEY)
    return merge_stored_roles(info.get("roles") or [], stored, member_id)


# --- API routes -----------------------------------------------------------------
#
# Mounted by main.py under /participants, so the public URLs are
# /participants/roles and /participants/{member_id}/role. They read as endpoints
# about participants, and the paths are part of the contract with auth-api and
# the other mirrors — moving the code in here must not move the URLs.

router = APIRouter()


class RolesUpdate(BaseModel):
    roles: list[str] = Field(default_factory=list)  # The full list, not a delta


# Callers allowed to read the whole role map. wsj27-auth-api needs it to mint
# tokens; other consumers mirror the roles into their own systems (the Discord
# bot grants server roles from them). Separate role names so one can be revoked
# without touching the other — auth-api's grant is infrastructural, the rest are
# ordinary consumers.
#
# Matched by prefix, so "wsj27:cmt:admin" covers every Roll within the Admin
# function rather than naming each one. The two service roles have no deeper
# segments, so for them a prefix match is an exact match.
ROLE_MAP_ROLES = frozenset({"wsj27:bulkread", "wsj27:rolereader", "wsj27:cmt:admin"})


@router.get(
    "/roles",
    status_code=status.HTTP_200_OK,
    response_description="WSJ27 roles for all participants",
    include_in_schema=False,
)
async def participant_roles(
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    user: AuthUser = Depends(require_auth_user),
) -> Response:
    """Every participant's WSJ27 roles, keyed by member number.

    This is the one interpretation of role assignment that all consumers share:
    auth-api mints these into tokens, and other services mirror them. Callers
    receive finished roles rather than the fields behind them, so the mapping
    rules exist in exactly one place.

    Carries no names, emails or other personal data — only member numbers and
    the roles they hold.

    Send the previous `ETag` back as `If-None-Match` to get a 304 when nothing
    has changed, which is the normal case between Scoutnet refreshes. The tag
    covers the *roles*, so an edit to a participant's name does not change it —
    only a change that actually affects someone's roles does.
    """
    if not user.has_any_role(ROLE_MAP_ROLES):
        logger.warning("Denied bulk participant access to %s", user)
        # 404 rather than 403: a caller that shouldn't know this exists learns nothing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    pdata = get_single_project()
    # Minted roles are computed once, at decode time, by scoutnet_forms.py;
    # hand-assigned ones are merged in here, from the scoutnet_db object, so a
    # write shows up on the next request without anything being re-derived.
    #
    # Members with no roles are omitted rather than sent as empty lists: the
    # meaning is identical and it keeps the body to the few hundred people who
    # actually hold a role, out of ~2600 participants.
    #
    # Keys are stringified because JSON object keys are strings either way; the
    # cache itself is keyed by int (see scoutnet_forms_decoder).
    participants = {
        str(member_id): member_roles
        for member_id, info in pdata.participants.items()
        if (member_roles := _all_roles(member_id, info))
    }

    # Hash the exact body we return, so the ETag cannot drift from the content.
    body = json.dumps({"participants": participants}, ensure_ascii=False, sort_keys=True)
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'

    if if_none_match and etag in {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    logger.info("Served roles for %d members to %s", len(participants), user)
    return Response(content=body, media_type="application/json", headers={"ETag": etag})


@router.post(
    "/{member_id}/role",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    response_description="The member's stored object after the write",
)
async def set_roles(
    member_id: int,
    update: RolesUpdate,
    user: AuthUser = Depends(require_auth_user),
):
    """Replace one participant's hand-assigned roles. Kontingentledning only.

    The whole list, not a delta: removing a role is just leaving it out. The
    change is in /roles as soon as this returns.
    """
    if not user.has_role("wsj27:cmt"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Kontingentledning may set roles.")
    if member_id not in get_single_project().participants:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Participant not found in project.")
    if bad := [role for role in update.roles if not is_assigned_role(role)]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Roles must be under 'wsj27:access:': {', '.join(bad)}",
        )

    logger.info("%s set roles %s for member %s", user, update.roles, member_id)
    try:
        return await scoutnet_db.set_values(member_id, {STORED_ROLES_KEY: update.roles or None})
    except scoutnet_db.ScoutnetDbError as exc:
        logger.error("Role write for member %s failed: %s", member_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not store in Scoutnet.") from exc
