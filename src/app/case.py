"""Case management: cases and the notes attached to them.

A case tracks an issue for the contingent through the notes added to it, until
it is closed. It is about one person (`about_person_id`) or, if that is left
out, about a troop as a whole.

Who may reach a case is decided by its `type`, its `secrecy_level` and its
`extra_access` list; the rules are spelled out above `_ROLE_SCOPE` below. Each
type in `CASE_TYPES` names the role it needs, and an `avdelning` case is also
limited to the leaders of its own troop. A case the caller may not reach answers
404, the same as one that does not exist. A closed case can be reopened, and
nothing else about it can be changed until it is.

Value rules (`type`, `secrecy_level` range) are deliberately enforced here in
code, not as DB CHECK constraints: the schema is still in flux, and
`db_init_tables()` only ever runs `CREATE TABLE IF NOT EXISTS`, so a
constraint baked in at creation time would silently go stale against an
already-existing table the next time a rule changes.

`secrecy_level` is on cases and notes; a note's may not be lower than its
case's. `extra_access` (scoutnet member IDs) is on cases only.

`extra_access` and `tags` are plain array columns directly on `cases` (and
`tags` on `case_notes` too), not normalized lookup tables — see
tags-as-plain-arrays in project memory for why. `PUT .../extra_access` and
`PUT .../tags` both replace the whole list; neither merges with what is
already there.

Notes are immutable once created, apart from their tags — there is no endpoint
to edit or delete one. `GET /{case_id}/notes` logs the read
(`case_note_read_log`, one row per call, not deduplicated); every other read,
including the case listing, does not.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow, db_transaction
from .scoutnet import get_single_project

settings = get_settings()
logger = logging.getLogger(__name__)

# Finite but extendable set of case types, each with the role a caller needs for it.
CASE_TYPES = {"hälsa": "wsj27:cmt:support:halsa", "cmt": "wsj27:cmt", "avdelning": "wsj27:al"}

# Who may see what, by secrecy_level (1-2 are not in use yet):
#   3  the case type's role holders, and anyone on the case's extra_access
#   4  the role holders only; the case cannot have extra_access
#   5  the creator (who must still hold the role), and anyone on extra_access
# A note's own level only restricts when it is above its case's: a level 4 note
# is hidden from extra_access (who cannot write one either), a level 5 note is
# for its author alone.
#
# The same rules as SQL over `cases c` / `case_notes n`, with $1-$3 from
# _scope_args(), for the list and tag queries. Keep the two in step.
_ROLE_SCOPE = (
    "((c.type = ANY($1) OR (c.type = 'avdelning' AND c.troop = ANY($2)))"
    " AND (c.secrecy_level < 5 OR c.creator_id = $3))"
)
_SCOPE = f"({_ROLE_SCOPE} OR (c.secrecy_level <> 4 AND $3 = ANY(c.extra_access)))"
_NOTE_SCOPE = (
    f"(n.secrecy_level <= c.secrecy_level OR (n.secrecy_level = 4 AND {_ROLE_SCOPE})"
    " OR (n.secrecy_level = 5 AND n.creator_id = $3))"
)


def _troops(user: AuthUser) -> list[str]:
    return sorted(user.role_suffixes("wsj27:al"))


def _types_for(user: AuthUser) -> list[str]:
    return [
        case_type
        for case_type, role in CASE_TYPES.items()
        if user.has_role(role) and (case_type != "avdelning" or _troops(user))
    ]


def _may_access(user: AuthUser, case_type: str, troop: str) -> bool:
    return case_type in _types_for(user) and (case_type != "avdelning" or troop in _troops(user))


def _role_access(user: AuthUser, case) -> bool:
    """Access through the case type's role rather than extra_access."""
    if not _may_access(user, case["type"], case["troop"]):
        return False
    return case["secrecy_level"] < 5 or case["creator_id"] == user.user_id


def _has_access(user: AuthUser, case) -> bool:
    return _role_access(user, case) or (case["secrecy_level"] != 4 and user.user_id in case["extra_access"])


def _may_read_note(user: AuthUser, case, note) -> bool:
    if note["secrecy_level"] <= case["secrecy_level"]:
        return True
    if note["secrecy_level"] == 4:
        return _role_access(user, case)
    return note["creator_id"] == user.user_id


def _check_secrecy(secrecy_level: int, extra_access: list[int]) -> None:
    if secrecy_level < 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Secrecy levels 1 and 2 are not in use yet"
        )
    if secrecy_level == 4 and extra_access:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A secrecy level 4 case cannot have extra_access"
        )


def _scope_args(user: AuthUser) -> list:
    return [[t for t in _types_for(user) if t != "avdelning"], _troops(user), user.user_id]


async def _require_case_user(user: AuthUser = Depends(require_auth_user)) -> AuthUser:
    if not _types_for(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to cases")
    return user


async def _case(case_id: int, user: AuthUser = Depends(_require_case_user)):
    """The case, if the caller may reach it."""
    row = await db_fetchrow("SELECT * FROM cases WHERE id = $1", case_id)
    if row is None or not _has_access(user, row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return row


async def _open_case(case=Depends(_case)):
    """The case, if it is also open to changes."""
    if case["closed"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is closed")
    return case


async def _role_case(case=Depends(_open_case), user: AuthUser = Depends(_require_case_user)):
    """Like _open_case, but extra_access is not enough: a grantee cannot pass access on."""
    if not _role_access(user, case):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the case type's own roles may do this")
    return case


# --- Database functions ---


async def db_init_tables() -> None:
    logger.info("Initializing case database tables")
    await db_execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id                BIGSERIAL    PRIMARY KEY,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            creator_id        BIGINT       NOT NULL,
            secrecy_level     SMALLINT     NOT NULL,
            title             TEXT         NOT NULL,
            type              TEXT         NOT NULL,
            about_person_id   BIGINT,
            assigned_to_id    BIGINT,
            troop             TEXT         NOT NULL,
            latest_note_at    TIMESTAMPTZ,
            closed            BOOLEAN      NOT NULL DEFAULT false,
            closed_at         TIMESTAMPTZ,
            closed_by_id      BIGINT,
            extra_access      BIGINT[]     NOT NULL DEFAULT '{}',
            tags              TEXT[]       NOT NULL DEFAULT '{}'
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS cases_about_person_idx  ON cases (about_person_id)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_assigned_to_idx   ON cases (assigned_to_id)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_troop_idx         ON cases (troop)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_created_at_idx    ON cases (created_at)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_closed_idx        ON cases (closed)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_type_idx          ON cases (type)")

    await db_execute("""
        CREATE TABLE IF NOT EXISTS case_notes (
            id             BIGSERIAL    PRIMARY KEY,
            case_id        BIGINT       NOT NULL REFERENCES cases (id),
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            creator_id     BIGINT       NOT NULL,
            secrecy_level  SMALLINT     NOT NULL,
            title          TEXT         NOT NULL,
            note           TEXT         NOT NULL,
            tags           TEXT[]       NOT NULL DEFAULT '{}'
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS case_notes_case_id_idx ON case_notes (case_id)")

    await db_execute("""
        CREATE TABLE IF NOT EXISTS case_note_read_log (
            id         BIGSERIAL    PRIMARY KEY,
            case_id    BIGINT       NOT NULL REFERENCES cases (id),
            reader_id  BIGINT       NOT NULL,
            timestamp  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS case_note_read_log_case_id_idx ON case_note_read_log (case_id)")

    await db_execute("CREATE INDEX IF NOT EXISTS cases_tags_idx      ON cases USING GIN (tags)")
    await db_execute("CREATE INDEX IF NOT EXISTS case_notes_tags_idx ON case_notes USING GIN (tags)")

    logger.info("Case database tables ready")


# --- Models ---


class CaseCreate(BaseModel):
    secrecy_level: int = Field(
        3,
        ge=1,
        le=5,
        description="1 (least secret) to 5 (most secret). Used in access control. 1 and 2 are not in use yet.",
    )
    title: str
    type: str = Field(
        description="One of the values from `GET /cases/types`. Used as a filter in searches and in access control."
    )
    about_person_id: int | None = Field(None, description="Member ID. Omit for a case about a troop.")
    troop: str = Field(
        description="Troop number or function name, as a string (e.g. the `<troop>` in the `wsj27:al:<troop>` role)."
    )
    extra_access: list[int] = Field(
        default_factory=list,
        description="Member IDs given access on top of the case type's roles. Not allowed on level 4.",
    )
    tags: list[str] = Field(default_factory=list, description="Free-form labels. `GET /cases/tags` lists those in use.")


class Case(BaseModel):
    id: int
    created_at: datetime
    creator_id: int
    secrecy_level: int
    title: str
    type: str
    about_person_id: int | None
    assigned_to_id: int | None
    troop: str
    latest_note_at: datetime | None
    closed: bool
    closed_at: datetime | None
    closed_by_id: int | None
    extra_access: list[int]
    tags: list[str]


class ExtraAccessUpdate(BaseModel):
    extra_access: list[int]


class TagsUpdate(BaseModel):
    tags: list[str]


class AssigneeUpdate(BaseModel):
    assigned_to_id: int | None


class NoteCreate(BaseModel):
    secrecy_level: int | None = Field(
        None, ge=1, le=5, description="Must be >= the case's own secrecy_level. Defaults to the case's."
    )
    title: str
    note: str
    tags: list[str] = Field(default_factory=list)


class Note(BaseModel):
    id: int
    case_id: int
    created_at: datetime
    creator_id: int
    secrecy_level: int
    title: str
    note: str
    tags: list[str]


# --- API routes ---

router = APIRouter(responses={403: {"description": "The caller's roles give no access to cases, or not to this."}})


@router.post(
    "",
    response_model=Case,
    status_code=status.HTTP_201_CREATED,
    summary="Create a case",
    description=(
        "Opens a new case. `about_person_id` may be omitted for a case about a "
        "troop as a whole rather than one person.\n\n"
        "`troop` is a string and can also hold a function name, e.g., CMT. "
        "For an `avdelning` case it must be one of the caller's own troops, and "
        "`about_person_id` must be someone in it."
    ),
    responses={
        201: {"description": "The created case."},
        403: {"description": "The caller may not create this type of case for this troop."},
        422: {
            "description": "Unknown `type`, a person outside the troop, secrecy level 1-2, or level 4 with `extra_access`."
        },
    },
)
async def create_case(case: CaseCreate, user: AuthUser = Depends(_require_case_user)):
    if case.type not in CASE_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Invalid case type: {case.type}")
    if not _may_access(user, case.type, case.troop):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"No access to {case.type} cases for {case.troop}"
        )
    if case.type == "avdelning" and case.about_person_id is not None:
        # Same answer for someone missing and someone in another troop, so a
        # leader cannot use this to find out who is in the contingent.
        person = get_single_project().participants.get(case.about_person_id)
        if not person or person["troop"] != case.troop:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Member {case.about_person_id} is not in troop {case.troop}",
            )
    _check_secrecy(case.secrecy_level, case.extra_access)

    row = await db_fetchrow(
        """
        INSERT INTO cases (creator_id, secrecy_level, title, type, about_person_id, troop, extra_access, tags)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        user.user_id,
        case.secrecy_level,
        case.title,
        case.type,
        case.about_person_id,
        case.troop,
        case.extra_access,
        case.tags,
    )
    return Case(**row)


@router.get(
    "",
    response_model=list[Case],
    status_code=status.HTTP_200_OK,
    summary="List cases",
    description=(
        "Newest first, filtered by whichever of the query parameters are given. "
        "`tag` matches cases carrying that exact tag. Closed cases are left out "
        "unless `include_closed=true`."
    ),
    responses={200: {"description": "The matching cases."}},
)
async def list_cases(
    about_person_id: int | None = None,
    troop: str | None = None,
    type: str | None = None,
    tag: str | None = None,
    not_older_than: datetime | None = None,
    include_closed: bool = False,
    user: AuthUser = Depends(_require_case_user),
):
    args = _scope_args(user)
    conditions = [_SCOPE]

    if about_person_id is not None:
        args.append(about_person_id)
        conditions.append(f"c.about_person_id = ${len(args)}")
    if troop is not None:
        args.append(troop)
        conditions.append(f"c.troop = ${len(args)}")
    if type is not None:
        args.append(type)
        conditions.append(f"c.type = ${len(args)}")
    if not_older_than is not None:
        args.append(not_older_than)
        conditions.append(f"c.created_at >= ${len(args)}")
    if not include_closed:
        conditions.append("NOT c.closed")
    if tag is not None:
        args.append(tag)
        conditions.append(f"c.tags @> ARRAY[${len(args)}]::TEXT[]")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = await db_fetch(
        f"SELECT c.* FROM cases c {where_clause} ORDER BY c.created_at DESC",
        *args,
    )
    return [Case(**row) for row in rows]


@router.post(
    "/{case_id}/close",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Close a case",
    description="Sets `closed`, `closed_at` and `closed_by_id` (to the caller). Closed cases reject new notes.",
    responses={
        200: {"description": "The closed case."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is already closed."},
    },
)
async def close_case(case_id: int, case=Depends(_open_case), user: AuthUser = Depends(_require_case_user)):
    row = await db_fetchrow(
        """
        UPDATE cases
        SET closed = true, closed_at = NOW(), closed_by_id = $2
        WHERE id = $1 AND NOT closed
        RETURNING *
        """,
        case_id,
        user.user_id,
    )
    if row is None:  # closed since it was looked up
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is closed")
    return Case(**row)


@router.post(
    "/{case_id}/reopen",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Reopen a case",
    description="Clears `closed`, `closed_at` and `closed_by_id`.",
    responses={
        200: {"description": "The reopened case."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is not closed."},
    },
)
async def reopen_case(case_id: int, case=Depends(_case)):
    if not case["closed"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is not closed")
    row = await db_fetchrow(
        """
        UPDATE cases
        SET closed = false, closed_at = NULL, closed_by_id = NULL
        WHERE id = $1 AND closed
        RETURNING *
        """,
        case_id,
    )
    if row is None:  # reopened since it was looked up
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is not closed")
    return Case(**row)


@router.post(
    "/{case_id}/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    summary="Add a note to a case",
    description=(
        "Also bumps the case's `latest_note_at`. Rejected if the case is closed, "
        "or if the note's `secrecy_level` is lower than the case's. Only the "
        "case type's role holders may write a level 4 note."
    ),
    responses={
        201: {"description": "The created note."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is closed."},
        422: {"description": "The note's secrecy_level is lower than the case's."},
    },
)
async def create_note(
    case_id: int, note: NoteCreate, case=Depends(_open_case), user: AuthUser = Depends(_require_case_user)
):
    secrecy_level = case["secrecy_level"] if note.secrecy_level is None else note.secrecy_level
    if secrecy_level < case["secrecy_level"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Note secrecy level must be equal or higher than the case's secrecy level",
        )
    if secrecy_level == 4 and not _role_access(user, case):
        # Level 4 keeps grantees out; one of them must not slip notes past the others.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the case type's own roles may do this")

    async with db_transaction() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO case_notes (case_id, creator_id, secrecy_level, title, note, tags)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            case_id,
            user.user_id,
            secrecy_level,
            note.title,
            note.note,
            note.tags,
        )
        await conn.execute(
            "UPDATE cases SET latest_note_at = $2 WHERE id = $1",
            case_id,
            row["created_at"],
        )
        return Note(**row)


@router.get(
    "/{case_id}/notes",
    response_model=list[Note],
    status_code=status.HTTP_200_OK,
    summary="List a case's notes",
    description=(
        "Newest first. Unlike other reads in this API, this one is logged — each "
        "call adds a row to `case_note_read_log` for this case and caller, "
        "regardless of whether there are new notes to see."
    ),
    responses={
        200: {"description": "The case's notes."},
        404: {"description": "No case with this id."},
    },
)
async def get_case_notes(case_id: int, case=Depends(_case), user: AuthUser = Depends(_require_case_user)):
    rows = await db_fetch("SELECT * FROM case_notes WHERE case_id = $1 ORDER BY created_at DESC", case_id)
    await db_execute(
        "INSERT INTO case_note_read_log (case_id, reader_id) VALUES ($1, $2)",
        case_id,
        user.user_id,
    )
    return [Note(**row) for row in rows if _may_read_note(user, case, row)]


@router.put(
    "/{case_id}/extra_access",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Replace a case's extra_access list",
    description="Whole-list replace, not a merge — send the complete list of member IDs to grant access.",
    responses={
        200: {"description": "The case with its extra_access list replaced."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is closed."},
        422: {"description": "The case is secrecy level 4, which cannot have extra_access."},
    },
)
async def update_case_extra_access(case_id: int, update: ExtraAccessUpdate, case=Depends(_role_case)):
    _check_secrecy(case["secrecy_level"], update.extra_access)
    row = await db_fetchrow(
        "UPDATE cases SET extra_access = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.extra_access,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/assignee",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Set or clear a case's assignee",
    description="Single assignee. Pass `assigned_to_id: null` to unassign.",
    responses={
        200: {"description": "The case with its assignee updated."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is closed."},
    },
)
async def update_case_assignee(case_id: int, update: AssigneeUpdate, case=Depends(_open_case)):
    row = await db_fetchrow(
        "UPDATE cases SET assigned_to_id = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.assigned_to_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/tags",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Replace a case's tags",
    description="Whole-list replace, not a merge — send the complete list of tags the case should carry.",
    responses={
        200: {"description": "The case with its tags replaced."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is closed."},
    },
)
async def update_case_tags(case_id: int, update: TagsUpdate, case=Depends(_open_case)):
    row = await db_fetchrow(
        "UPDATE cases SET tags = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.tags,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/notes/{note_id}/tags",
    response_model=Note,
    status_code=status.HTTP_200_OK,
    summary="Replace a note's tags",
    description="Whole-list replace, not a merge — send the complete list of tags the note should carry.",
    responses={
        200: {"description": "The note with its tags replaced."},
        404: {"description": "No note with this id on this case that the caller may read."},
        409: {"description": "The case is closed."},
    },
)
async def update_note_tags(
    case_id: int,
    note_id: int,
    update: TagsUpdate,
    case=Depends(_open_case),
    user: AuthUser = Depends(_require_case_user),
):
    note = await db_fetchrow("SELECT * FROM case_notes WHERE id = $2 AND case_id = $1", case_id, note_id)
    if note is None or not _may_read_note(user, case, note):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    row = await db_fetchrow(
        "UPDATE case_notes SET tags = $3 WHERE id = $2 AND case_id = $1 RETURNING *",
        case_id,
        note_id,
        update.tags,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return Note(**row)


@router.get(
    "/tags",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    summary="List all tags in use",
    description="Distinct tag names currently applied to any case or note, alphabetically.",
    responses={200: {"description": "All existing tag names."}},
)
async def list_tags(user: AuthUser = Depends(_require_case_user)):
    rows = await db_fetch(
        f"""
        SELECT DISTINCT tag FROM (
            SELECT unnest(c.tags) AS tag FROM cases c WHERE {_SCOPE}
            UNION
            SELECT unnest(n.tags) AS tag FROM case_notes n JOIN cases c ON c.id = n.case_id
            WHERE {_SCOPE} AND {_NOTE_SCOPE}
        ) all_tags
        ORDER BY tag
        """,
        *_scope_args(user),
    )
    return [row["tag"] for row in rows]


@router.get(
    "/types",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    summary="List valid case types",
    description=("The values `type` may take on `POST /cases` for this caller: the types their roles give them."),
    responses={200: {"description": "The caller's case types."}},
)
async def list_case_types(user: AuthUser = Depends(_require_case_user)):
    return _types_for(user)
