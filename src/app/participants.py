"""Participant lookup, at three levels of detail, and the patrol write.

Every route is gated twice: `require_auth_user` says who is asking, and the
rules below say how much of the answer they get. "name" and "basic" need basic
access; "full" adds the health and dietary answers, so it needs a grant of its
own.

  * Avdelningsledare (`wsj27:al:<troop>`) may read their own troop at any level
    and nothing else. A health role raises how much of their troop they see,
    never whose records they can reach.
  * Kontingentledning (`wsj27:cmt...`) may read every participant at basic, and
    full with either of the health roles below.

Both can be true of one person, and then each rule applies where it applies.

Two rules cut across the levels, both about adults' own records. A participant
who is themselves an Avdelningsledare keeps their contact_info and forms_data
out of every response except to Kontingentledning with health authorisation — a
leader reading their own troop sees the young people in full and their fellow
leaders as names. And a participant who is Kontingentledning keeps their
forms_data for holders of `wsj27:legacy-access:Hälsa plus intern information` alone —
though as of 2026-09-20 it is temporarily withheld from every caller, see
`_withheld()`.
"""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from . import scoutnet_db
from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .scoutnet import get_single_project

# --- Settings and data classes ---

settings = get_settings()
logger = logging.getLogger(__name__)

InfoLevel = Literal["name", "basic", "full"]

TROOP_MAPPER = {"cmt": "Kontingentledning", "al": "Avdelningsledare", "ist": "IST"}

# Ordered, so `granted < required` is the whole check. A name is participant
# data like any other, hence the same cost as basic: someone with no access to a
# person must not be able to confirm they exist by asking for their name.
NO_ACCESS, BASIC_ACCESS, FULL_ACCESS = 0, 1, 2
REQUIRED_ACCESS = {"name": BASIC_ACCESS, "basic": BASIC_ACCESS, "full": FULL_ACCESS}

# Either one unlocks health data. The first is the Support function's health
# people, slugified out of the Funktion/Roll CSV ("Hälsa" -> "halsa") - this
# also covers "Hälsa PL", since roles.py drops that suffix and mints the same
# role for both. The second is granted per person in the Scoutnet form and cuts
# across functions; it is also the only one that reaches Kontingentledning's own
# health answers, so it is named on its own as well.
CMT_HEALTH_ROLE = "wsj27:cmt:support:halsa"
INTERNAL_INFO_ROLE = "wsj27:legacy-access:Hälsa plus intern information"
HEALTH_ROLES = frozenset({CMT_HEALTH_ROLE, INTERNAL_INFO_ROLE})

NOT_FOUND = "Participant not found in project."


class PatrolUpdate(BaseModel):
    patrol: str | None = None  # None or "" removes the member's patrol


# --- Internal helpers ---


def _troop_access(user: AuthUser, troop: str | None) -> int:
    """How much of `troop`'s participants this caller may see.

    Pass None for a collection that is no troop — a member-type listing — so
    that only the contingent-wide grant can apply. Holding no leader troops
    means no troops, never all of them.
    """
    if troop and troop in user.role_suffixes("wsj27:al"):
        return FULL_ACCESS
    if not user.has_role("wsj27:cmt"):
        return NO_ACCESS
    return FULL_ACCESS if user.has_any_role(HEALTH_ROLES) else BASIC_ACCESS


def _authorize(user: AuthUser, troop: str | None, infolevel: InfoLevel, subject: str, not_found: str) -> None:
    """Raise unless the caller may read `troop` at `infolevel`.

    No access at all is a 404 carrying `not_found` — the same answer a record
    that genuinely is not there gets, so a refusal cannot be used to discover
    who exists. Access but not this much of it is a 403 rather than a quietly
    downgraded 200: a client has to be able to tell "no health answers
    recorded" from "you may not see them".
    """
    granted = _troop_access(user, troop)
    if granted == NO_ACCESS:
        logger.warning("Denied %s access to %s", user, subject)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)
    if granted < REQUIRED_ACCESS[infolevel]:
        logger.warning("Denied %s '%s' access to %s", user, infolevel, subject)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Info level '{infolevel}' requires authorisation for health and internal information.",
        )


def _withheld(user: AuthUser) -> dict[str, set[str]]:
    """Fields this caller does not get, keyed by the *participant's* member_type.

    Both entries are about who the participant is rather than which troop they
    are in, so the caller's side is answered once per request and applied row by
    row.

      * An Avdelningsledare's contact_info and forms_data are for
        Kontingentledning with health authorisation only, which is precisely the
        contingent-wide full grant — so ask for it with no troop, leaving the
        caller's own troop out of it. Being a leader of the troop is what does
        *not* count here, and passing None is what makes sure it cannot.
      * Kontingentledning's own forms_data needs the per-person grant from the
        Scoutnet form — `INTERNAL_INFO_ROLE`. The Support function's health role
        is deliberately not enough: it covers the contingent's health work, not
        the contingent leadership's own answers. **Withheld from everyone for
        now**, see below.
    """
    withheld: dict[str, set[str]] = {}
    if _troop_access(user, None) != FULL_ACCESS:
        withheld["Avdelningsledare"] = {"contact_info", "forms_data"}

    # TEMPORARY (2026-09-20): nobody reads Kontingentledning's own health
    # answers, not even INTERNAL_INFO_ROLE. To restore the permanent rule, put
    # `if not user.has_role(INTERNAL_INFO_ROLE):` back in front of the line
    # below; the suite then names the three test changes that go with it (drop
    # the xfail marker, and drop HEALTH_ACCESS from CMT_HEALTH_CALLERS).
    #
    # Deliberately *only* this line: INTERNAL_INFO_ROLE is also half of
    # HEALTH_ROLES, so disabling the role itself would revoke `infolevel=full`
    # contingent-wide for everyone who has no other health grant.
    withheld["Kontingentledning"] = {"forms_data"}
    return withheld


def _project(participant: dict[str, Any], infolevel: InfoLevel, withheld: dict[str, set[str]]) -> dict[str, Any]:
    """One participant cut down to `infolevel`, always as a new dict.

    Adults' own details are cut further than the level asked for: a leader
    reading their own troop gets the young people in full but their fellow
    leaders stripped of contact_info and forms_data, and Kontingentledning's
    health answers go only to the internal-information role. Dropped from the
    record rather than refused, because a listing mixes both kinds of
    participant and a 403 would take the whole list down over one row.
    """
    if infolevel == "name":
        return {"member_no": participant["member_no"], "name": participant["name"]}

    drop = set() if infolevel == "full" else {"forms_data"}
    drop |= withheld.get(participant["member_type"], set())
    return {key: value for key, value in participant.items() if key not in drop}


# --- API routes ---

router = APIRouter()


@router.get(
    "/troopinfo/{troop_id}",
    response_model=list,
    status_code=status.HTTP_200_OK,
    response_description="Troop info",
)
async def troopinfo(
    troop_id: str,
    infolevel: InfoLevel = Query("basic"),
    user: AuthUser = Depends(require_auth_user),
):
    """Every participant in one troop, or in one member type."""
    troop_id = TROOP_MAPPER.get(troop_id, troop_id)

    # Authorised before the lookup, so an unauthorised caller gets the same
    # answer whether or not the troop exists. Only a numbered troop can be a
    # leader's own; a member-type listing is nobody's troop, hence None.
    _authorize(
        user, troop_id if troop_id.isdigit() else None, infolevel, f"troop {troop_id}", "Troop not found in project."
    )

    pdata = get_single_project()
    if troop_id.isdigit():
        tinfo = [p for p in pdata.participants.values() if p["troop"] == troop_id]
    elif troop_id != "Deltagare":
        tinfo = [p for p in pdata.participants.values() if p["member_type"] == troop_id]
    else:
        tinfo = []

    if not tinfo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Troop not found in project.",
        )

    withheld = _withheld(user)
    return [_project(p, infolevel, withheld) for p in tinfo]


@router.get(
    "/individual/{member_id}",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    response_description="Individual info",
)
async def individualinfo(
    member_id: int,
    infolevel: InfoLevel = Query("basic"),
    user: AuthUser = Depends(require_auth_user),
):
    """One participant, by member number."""
    pdata = get_single_project()
    meminfo = pdata.participants.get(member_id)
    if not meminfo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=NOT_FOUND,
        )

    # The participant's own troop is what decides this, so unlike troopinfo the
    # lookup has to come first. Same 404 detail as above, for the same reason.
    _authorize(user, meminfo["troop"], infolevel, f"member {member_id}", NOT_FOUND)

    if infolevel == "name":
        # Narrower than "name" elsewhere: the caller asked by member number, so
        # the number is not news. Kept as it was, to not move a response shape.
        return {"name": meminfo["name"]}

    return _project(meminfo, infolevel, _withheld(user))


@router.post(
    "/{member_id}/patrol",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    response_description="The member's stored object after the write",
)
async def set_patrol(
    member_id: int,
    update: PatrolUpdate,
    user: AuthUser = Depends(require_auth_user),
):
    """Set (or clear) one participant's patrol. Only for the leader of their troop.

    `role_suffixes`, not `has_role("wsj27:al")`: a leader of troop 17 may write
    to troop 17 and nothing else.
    """
    meminfo = get_single_project().participants.get(member_id)
    if not meminfo or meminfo["troop"] not in user.role_suffixes("wsj27:al"):
        logger.warning("Denied %s patrol write to member %s", user, member_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)

    logger.info("%s set patrol %r for member %s", user, update.patrol, member_id)
    try:
        return await scoutnet_db.set_values(member_id, {"patrol": update.patrol or None})
    except scoutnet_db.ScoutnetDbError as exc:
        logger.error("Patrol write for member %s failed: %s", member_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not store in Scoutnet.") from exc
