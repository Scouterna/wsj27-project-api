# wsj27-project-api

Project data API for the WSJ27 apps: participant and role data derived from
Scoutnet, plus an optional case-management feature for the health/safety team.

This is early-stage, pre-1.0 code, published to share what exists so far — not
a finished product. Expect rough edges and breaking changes.

## What it does

- Fetches project, participant and form data from the Scoutnet project API and
  caches it in memory (`src/app/scoutnet.py`).
- Decodes each participant's raw form answers into a stable, keyed
  `forms_data` structure per a hand-maintained template — see
  [docs/forms-data-decoder.md](docs/forms-data-decoder.md) and
  [docs/scoutnet-data-quirks.md](docs/scoutnet-data-quirks.md).
- Derives WSJ27 roles for each participant (`src/app/roles.py`), served at
  `GET /participants/roles` for [wsj27-auth-api](https://github.com/Scouterna/wsj27-auth-api)
  and other consumers to mint into tokens.
- Optionally (`POSTGRES_DSN` set) exposes a case-management API under
  `/cases` for tracking health/safety follow-ups tied to a member.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Health check, no auth. |
| `GET /participants/troopinfo/{troop_id}` | Participants in a troop/unit. |
| `GET /participants/individual/{member_id}` | A single participant. |
| `GET /participants/roles` | `member_no -> roles` map for auth-api and other consumers. |
| `GET /scoutnet/refresh` | Force a Scoutnet cache refresh. |
| `/cases/*` | Case management — only registered when `POSTGRES_DSN` is set. |
| `GET /docs` | Swagger UI (also `/redoc`, `/openapi.json`). |

Auth follows the same model as the rest of the WSJ27 apps: a
`wsj27-auth_access-token` cookie or bearer token, validated as a standard JWT
against wsj27-auth-api's JWKS. Set `AUTH_DISABLED=true` locally to skip this
(see `.env.example`), and `FAKE_USER_ROLES` to choose who you are while it is.

## Who may read participant data

Authentication is only the first gate. What comes back then depends on the
caller's roles and on the `infolevel` asked for — `name` and `basic` need basic
access, `full` additionally carries the health and dietary answers. The rules
live at the top of `src/app/participants.py`:

| Caller | `name` / `basic` | `full` |
|---|---|---|
| Avdelningsledare (`wsj27:al:<troop>`) | Own troop only | Own troop only |
| Kontingentledning (`wsj27:cmt...`) | Every participant | With `wsj27:cmt:support:halsa` or `wsj27:access:Hälsa plus intern information` |
| Anyone else | — | — |

A leader's authority is their troop and stops there: an access role raises how
much they see of their own troop, never whose records they can reach. Being both
a leader and in the CMT adds up — full over the own troop, CMT rules elsewhere.

Two rules cut across the table, both about adults' own records, and both drop
fields from the response rather than refusing it — a listing mixes both kinds of
participant, and a 403 would take the whole list down over one row. Neither has
an exception for the caller's own record.

- A participant who is themselves an `Avdelningsledare` has their `contact_info`
  and `forms_data` withheld from every caller except Kontingentledning with
  health authorisation. So a leader reading their own troop sees the young
  people in full and their fellow leaders without those two fields.
- A participant who is `Kontingentledning` has their `forms_data` withheld from
  everyone but holders of `wsj27:access:Hälsa plus intern information`. The
  Support function's `wsj27:cmt:support:halsa` is deliberately not enough here:
  it covers the contingent's health work, not the contingent leadership's own
  answers.

Refusals come in two kinds. A caller with no access to a record gets **404**,
with the same body a record that does not exist returns, so a refusal cannot be
used to discover who is in the contingent. A caller who may see the record but
not at that level gets **403** rather than a quietly downgraded 200 — a client
has to be able to tell "no health answers recorded" from "not allowed to see
them".

## Running locally

```bash
uv sync
cp .env.example src/.env      # fill in SCOUTNET_PROJECTS at minimum
cd src && uv run python start.py
```

Leave `POSTGRES_DSN` empty to run without a database — the app still starts
and serves participants, roles and Scoutnet data; only `/cases` is unavailable.

With docker-compose (brings up Postgres too):

```bash
cp .env.example src/.env
podman compose up --build
```

See `.env.example` for every setting.
