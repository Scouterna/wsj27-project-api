"""Case management: cases and the notes attached to them.

A case tracks an issue for the contingent through the notes added to it, until
it is closed. It is about one person (`about_person_id`) or, if that is left
out, about a troop as a whole. `type` is drawn from the small, code-extendable
`CASE_TYPES` list below; it is meant to eventually gate who may access a case,
but that mapping is not implemented yet — today `type` only constrains what a
caller may write, via the check in `create_case`. This is deliberately not a
DB-level CHECK constraint: `CASE_TYPES` is expected to change over time, and
`db_init_tables()` only ever runs `CREATE TABLE IF NOT EXISTS`, so a
constraint baked in at creation time would silently go stale against an
already-existing table the next time the list changes.

`secrecy_level` (1-5) and `extra_access` (a list of scoutnet member IDs) exist
on both cases and notes for the same reason: an access model to build on top
of. Neither is enforced yet either. The one existing rule that touches them is
in `create_note`: a note's own `secrecy_level` may not be set lower than its
case's. `require_auth_user` gates every route below on being signed in, but
nothing here yet limits *which* caller may read or write a given case.

`extra_access` and `tags` are plain array columns directly on `cases` and
`case_notes` (not normalized lookup tables) — see tags-as-plain-arrays in
project memory for why. `PUT .../extra_access` and `PUT .../tags` both replace
the whole list; neither merges with what is already there.

Notes are otherwise immutable once created — there is no endpoint to edit or
delete one. `GET /{case_id}/notes` logs the read (`case_note_read_log`, one row
per call, not deduplicated); every other read, including the case listing,
does not.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow, db_transaction

settings = get_settings()
logger = logging.getLogger(__name__)

# Finite but extendable set of case types; each sets up a basic access level for the
# case. The type -> access-level mapping itself is not implemented yet.
CASE_TYPES = ["hälsa", "admin", "avdelning"]


# --- Database functions ---


async def db_init_tables() -> None:
    logger.info("Initializing case database tables")
    await db_execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id                BIGSERIAL    PRIMARY KEY,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            creator_id        BIGINT       NOT NULL,
            secrecy_level     SMALLINT     NOT NULL CHECK (secrecy_level BETWEEN 1 AND 5),
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
            secrecy_level  SMALLINT     NOT NULL CHECK (secrecy_level BETWEEN 1 AND 5),
            title          TEXT         NOT NULL,
            note           TEXT         NOT NULL,
            extra_access   BIGINT[]     NOT NULL DEFAULT '{}',
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
    secrecy_level: int = Field(ge=1, le=5, description="1 (least secret) to 5 (most secret). Used in access control.")
    title: str
    type: str = Field(
        description="One of the values from `GET /cases/types`. Used as a filer in searches and enforces access control."
    )
    about_person_id: int | None = Field(None, description="Member ID. Omit for a case about aq troop.")
    troop: str = Field(
        description="Troop number or function name, as a string (e.g. the `<troop>` in the `wsj27:al:<troop>` role)."
    )
    extra_access: list[int] = Field(
        default_factory=list, description="Extra access above the deafult. Add the member IDs."
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
    secrecy_level: int = Field(ge=1, le=5, description="Must be >= the case's own secrecy_level.")
    title: str
    note: str
    extra_access: list[int] = Field(default_factory=list, description="Scoutnet member IDs. Not yet enforced.")
    tags: list[str] = Field(default_factory=list)


class Note(BaseModel):
    id: int
    case_id: int
    created_at: datetime
    creator_id: int
    secrecy_level: int
    title: str
    note: str
    extra_access: list[int]
    tags: list[str]


# --- API routes ---

router = APIRouter()


@router.post(
    "",
    response_model=Case,
    status_code=status.HTTP_201_CREATED,
    summary="Create a case",
    description=(
        "Opens a new case. `about_person_id` may be omitted for a case about a "
        "troop as a whole rather than one person.\n\n"
        "`troop` is a string and can also hold a function name, e.g., CMT."
        "The `troop` name/number is however used in searches and in applying"
        "access control, so some restrictions will be applied in the future."
    ),
    responses={
        201: {"description": "The created case."},
        422: {"description": "`type` is not one of the values from `GET /cases/types`."},
    },
)
async def create_case(case: CaseCreate, user: AuthUser = Depends(require_auth_user)):
    if case.type not in CASE_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Invalid case type: {case.type}")

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
    user: AuthUser = Depends(require_auth_user),
):
    conditions = []
    args = []

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
async def close_case(case_id: int, user: AuthUser = Depends(require_auth_user)):
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
    if row is None:
        existing = await db_fetchrow("SELECT id FROM cases WHERE id = $1", case_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is already closed")
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
async def reopen_case(case_id: int, user: AuthUser = Depends(require_auth_user)):
    row = await db_fetchrow(
        """
        UPDATE cases
        SET closed = false, closed_at = NULL, closed_by_id = NULL
        WHERE id = $1 AND closed
        RETURNING *
        """,
        case_id,
    )
    if row is None:
        existing = await db_fetchrow("SELECT id FROM cases WHERE id = $1", case_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is not closed")
    return Case(**row)


@router.post(
    "/{case_id}/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    summary="Add a note to a case",
    description=(
        "Also bumps the case's `latest_note_at`. Rejected if the case is closed, "
        "or if the note's `secrecy_level` is lower than the case's."
    ),
    responses={
        201: {"description": "The created note."},
        404: {"description": "No case with this id."},
        409: {"description": "The case is closed."},
        422: {"description": "The note's secrecy_level is lower than the case's."},
    },
)
async def create_note(case_id: int, note: NoteCreate, user: AuthUser = Depends(require_auth_user)):
    case_row = await db_fetchrow("SELECT secrecy_level, closed FROM cases WHERE id = $1", case_id)
    if case_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    if case_row["closed"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is closed")
    if note.secrecy_level < case_row["secrecy_level"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Note secrecy level must be equal or higher than the case's secrecy level",
        )

    async with db_transaction() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO case_notes (case_id, creator_id, secrecy_level, title, note, extra_access, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            case_id,
            user.user_id,
            note.secrecy_level,
            note.title,
            note.note,
            note.extra_access,
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
async def get_case_notes(case_id: int, user: AuthUser = Depends(require_auth_user)):
    case_row = await db_fetchrow("SELECT id FROM cases WHERE id = $1", case_id)
    if case_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")

    rows = await db_fetch("SELECT * FROM case_notes WHERE case_id = $1 ORDER BY created_at DESC", case_id)
    await db_execute(
        "INSERT INTO case_note_read_log (case_id, reader_id) VALUES ($1, $2)",
        case_id,
        user.user_id,
    )
    return [Note(**row) for row in rows]


@router.put(
    "/{case_id}/extra_access",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Replace a case's extra_access list",
    description="Whole-list replace, not a merge — send the complete list of member IDs to grant access.",
    responses={
        200: {"description": "The case with its extra_access list replaced."},
        404: {"description": "No case with this id."},
    },
)
async def update_case_extra_access(
    case_id: int, update: ExtraAccessUpdate, user: AuthUser = Depends(require_auth_user)
):
    row = await db_fetchrow(
        "UPDATE cases SET extra_access = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.extra_access,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/notes/{note_id}/extra_access",
    response_model=Note,
    status_code=status.HTTP_200_OK,
    summary="Replace a note's extra_access list",
    description="Whole-list replace, not a merge — send the complete list of member IDs to grant access.",
    responses={
        200: {"description": "The note with its extra_access list replaced."},
        404: {"description": "No note with this id on this case."},
    },
)
async def update_note_extra_access(
    case_id: int, note_id: int, update: ExtraAccessUpdate, user: AuthUser = Depends(require_auth_user)
):
    row = await db_fetchrow(
        "UPDATE case_notes SET extra_access = $3 WHERE id = $2 AND case_id = $1 RETURNING *",
        case_id,
        note_id,
        update.extra_access,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return Note(**row)


@router.put(
    "/{case_id}/assignee",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    summary="Set or clear a case's assignee",
    description="Single assignee. Pass `assigned_to_id: null` to unassign.",
    responses={
        200: {"description": "The case with its assignee updated."},
        404: {"description": "No case with this id."},
    },
)
async def update_case_assignee(case_id: int, update: AssigneeUpdate, user: AuthUser = Depends(require_auth_user)):
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
    },
)
async def update_case_tags(case_id: int, update: TagsUpdate, user: AuthUser = Depends(require_auth_user)):
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
        404: {"description": "No note with this id on this case."},
    },
)
async def update_note_tags(case_id: int, note_id: int, update: TagsUpdate, user: AuthUser = Depends(require_auth_user)):
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
async def list_tags(user: AuthUser = Depends(require_auth_user)):
    rows = await db_fetch("""
        SELECT DISTINCT tag FROM (
            SELECT unnest(tags) AS tag FROM cases
            UNION
            SELECT unnest(tags) AS tag FROM case_notes
        ) all_tags
        ORDER BY tag
    """)
    return [row["tag"] for row in rows]


@router.get(
    "/types",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    summary="List valid case types",
    description=(
        "The values `type` may take on `POST /cases`. Each is meant to eventually "
        "set a case's default access level, but that mapping is not implemented yet."
    ),
    responses={200: {"description": "All valid case types."}},
)
async def list_case_types(user: AuthUser = Depends(require_auth_user)):
    return CASE_TYPES
