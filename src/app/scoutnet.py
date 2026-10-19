import asyncio
import functools
import hashlib
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from .authenctication import AuthUser, require_auth_user
from .config import ProjectConfig, get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# Suppress asyncio slow task warnings that leak response data
logging.getLogger("asyncio").setLevel(logging.ERROR)

PROJECT_API = "https://www.scoutnet.se/api/project/get"
CHECKIN_API = "https://www.scoutnet.se/api/project/checkin"
REFRESH_RETRY_MIN = 60  # Seconds to wait after the first failed refresh
REFRESH_RETRY_MAX = 1800  # Ceiling for the doubling retry delay
CACHE_DIR = Path(".dev_cache")
CACHE_FILE = settings.PERSIST_DIR / "project_cache.json"
OVERLAY_FILE = settings.PERSIST_DIR / "scoutnet_overlay.json"


class ScoutnetRequestError(RuntimeError):
    pass


# --- Data classes ---


@dataclass
class ScoutnetProjectData:
    """Raw data fetched from Scoutnet for one project."""

    project_id: int
    project_name: str
    groups: dict  # Empty dict if project has no group_key
    participants: dict
    questions: dict  # Combined: {"sections": {...}, "questions": {...}}


@dataclass
class CachedGroup:
    """Decoded data for a single group within a project."""

    id: int
    name: str
    num_participants: int = 0
    aggregated: dict = field(default_factory=dict)  # section_title -> {question_key: counts/values}
    raw_individual_answers: dict = field(default_factory=dict)  # member_no -> {question_key: value}
    raw_group_answers: dict = field(default_factory=dict)  # question_key -> raw value
    contact: dict | None = None


@dataclass
class CachedProject:
    """Decoded data for a single Scoutnet project."""

    project_id: int
    project_name: str
    participants: dict = field(default_factory=dict)  # member_no -> {name, born, ...}
    questions: dict = field(default_factory=dict)  # decoded questions dict from Scoutnet
    groups: dict = field(default_factory=dict)  # group_id -> CachedGroup


@dataclass
class ProjectCache:
    """Global cache for decoded Scoutnet project data."""

    projects: dict = field(default_factory=dict)  # project_id -> CachedProject


# --- Globals ---

_project_cache = ProjectCache()  # Project cache
_refresh_task: asyncio.Task | None = None  # Scheduled cache refresh task
# SCOUTNET_OVERLAY only. "<member_no>" -> {question_id: {"value": ..., "base": ...}},
# the same shape as OVERLAY_FILE. See "Write overlay" below.
_overlay: dict[str, dict[str, dict]] = {}
# SCOUTNET_OVERLAY only: the raw answers of the last fetch, member_no -> {qid: value},
# where a new overlay entry looks up the Scoutnet value it is laid over.
_fetched_answers: dict[int, dict] = {}


# --- Disk cache persistence ---


def _save_cache_to_disk(path: Path) -> None:
    from dataclasses import asdict

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(_project_cache)))
        tmp.rename(path)
        logger.info("Saved cache to disk: %d projects", len(_project_cache.projects))
    except Exception as exc:
        logger.warning("Failed to save cache to disk: %s", exc)


def _load_cache_from_disk(path: Path) -> bool:
    # return False
    try:
        data = json.loads(path.read_text())
        _project_cache.projects = {
            int(pid): CachedProject(
                project_id=p["project_id"],
                project_name=p["project_name"],
                # JSON object keys are always strings, so participants comes
                # back keyed by "3073781" where the live cache uses 3073781.
                # Convert back, or every lookup by member number misses for the
                # whole life of the pod. See scoutnet_forms_decoder().
                participants={int(mno): info for mno, info in p["participants"].items()},
                questions=p["questions"],
                # groups={int(gid): CachedGroup(**g) for gid, g in p["groups"].items()},
                groups={},
            )
            for pid, p in data["projects"].items()
        }
        logger.info("Loaded cache from disk: %d projects", len(_project_cache.projects))
        return True
    except Exception as exc:
        logger.warning("Failed to load cache from disk: %s", exc)
        return False


# --- Write overlay ---
#
# Every environment reads the one real Scoutnet project, and there is no test
# copy of it, so a write from dev or staging would change real data that prod,
# leaders and other tools read. With SCOUTNET_OVERLAY set, set_answer() never
# reaches Scoutnet: the value is kept here instead, and laid over the raw
# answers of every fetch before the forms decoder sees them. The environment
# then reads back exactly what it wrote, decoded exactly as prod would decode it.
#
# Each entry also keeps `base`, the Scoutnet value it was laid over. When a
# fetch brings a different value, Scoutnet has changed under the overlay (the
# IST patrol job, a hand-edit) and the real value wins: the entry is dropped.
# `base` is None when the write came before any fetch (a startup served from the
# disk cache); the next fetch then supplies it rather than dropping the entry.
#
# Saved to OVERLAY_FILE on every change, so it survives a restart, and a crashed
# pod loses nothing. Writes are rare, so this costs next to nothing. To reset an
# environment's test values, delete the file and restart. It assumes one
# replica; a scaled-out API would need the overlay in the database.


def _save_overlay() -> None:
    try:
        OVERLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OVERLAY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_overlay, ensure_ascii=False, indent=1, sort_keys=True))
        tmp.rename(OVERLAY_FILE)
    except OSError as exc:
        logger.error("Failed to save the Scoutnet overlay, it will not survive a restart: %s", exc)


def _load_overlay() -> None:
    global _overlay
    try:
        _overlay = json.loads(OVERLAY_FILE.read_text())
    except FileNotFoundError:
        _overlay = {}
    except (OSError, json.JSONDecodeError) as exc:
        # Starting with nothing is the safe way to fail: the environment reads
        # the real Scoutnet values and loses only its own test values.
        logger.error("Unreadable Scoutnet overlay %s, starting empty: %s", OVERLAY_FILE, exc)
        _overlay = {}
    count = sum(len(answers) for answers in _overlay.values())
    logger.warning(
        "SCOUTNET_OVERLAY is on — writes stay in %s (%d values), Scoutnet is never written", OVERLAY_FILE, count
    )


def _overlay_put(member_no: int, question_id: str, value: str) -> None:
    answers = _overlay.setdefault(str(member_no), {})
    if question_id in answers:
        base = answers[question_id]["base"]  # Still Scoutnet's value, or a fetch would have dropped it
    elif member_no in _fetched_answers:
        base = _fetched_answers[member_no].get(question_id) or ""
    else:
        base = None
    answers[question_id] = {"value": value, "base": base}
    _save_overlay()
    logger.info("Question %s for member %s kept in the overlay, not written to Scoutnet", question_id, member_no)


def _apply_overlay(pdata: dict) -> None:
    """Lay the overlay over one fetch's raw participants, in place.

    Drops the entries Scoutnet has moved away from, and the ones it now agrees
    with, which have nothing left to add.
    """
    global _fetched_answers
    _fetched_answers = {int(p["member_no"]): p["questions"] for p in pdata.values()}
    changed = False
    for member_key, answers in list(_overlay.items()):
        fetched = _fetched_answers.get(int(member_key))
        if fetched is None:
            continue  # Not in this fetch; kept in case they come back
        for question_id, entry in list(answers.items()):
            scoutnet_value = fetched.get(question_id) or ""
            if entry["base"] is None:
                entry["base"] = scoutnet_value
                changed = True
            if scoutnet_value != entry["base"] or scoutnet_value == entry["value"]:
                logger.info(
                    "Overlay value of question %s for member %s dropped, Scoutnet %s",
                    question_id,
                    member_key,
                    "now agrees" if scoutnet_value == entry["value"] else "has changed",
                )
                del answers[question_id]
                changed = True
                continue
            # This also changes _fetched_answers, which is the same dict. Harmless:
            # while the entry exists, a write to this question takes its base
            # from the entry, never from there.
            fetched[question_id] = entry["value"]
        if not answers:
            del _overlay[member_key]
    if changed:
        _save_overlay()


# --- Init / shutdown ---


def _next_refresh(now: datetime) -> datetime:
    """First refresh time after `now`, on the configured cadence.

    Runs are spaced out from 03:00 Europe/Stockholm, so the cadence divides the
    day the way one would expect: 24 h gives the old nightly 03:00 run, 1 h
    gives every hour on the hour, 6 h gives 03:00, 09:00, 15:00 and 21:00.
    """
    interval = timedelta(hours=settings.SCOUTNET_REFRESH_INTERVAL_HOURS)
    # Start a day back so the anchor itself can't make us skip earlier slots.
    next_run = now.replace(hour=3, minute=0, second=0, microsecond=0) - timedelta(days=1)
    while next_run <= now:
        next_run += interval
    return next_run


async def _scheduled_cache_refresh() -> None:
    logger.info("Scheduling cache refresh every %d h", settings.SCOUTNET_REFRESH_INTERVAL_HOURS)
    while True:
        now = datetime.now(tz=ZoneInfo("Europe/Stockholm"))
        await asyncio.sleep((_next_refresh(now) - now).total_seconds())
        logger.info("Running scheduled cache refresh")
        # Retry until successful, backing off from a minute to half an hour. A
        # blip should cost us a minute, not a whole refresh interval, but
        # Scoutnet is also down (or broken) for hours at a time, and there is no
        # point hammering it once it clearly isn't coming straight back. The
        # cache keeps serving the previous fetch throughout.
        delay = REFRESH_RETRY_MIN
        while True:
            try:
                await _update_project_cache()
                break
            except Exception:
                logger.error("Cache refresh failed, retrying in %d s", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, REFRESH_RETRY_MAX)


async def scoutnet_init() -> None:
    global _refresh_task
    if settings.SCOUTNET_DEV_CACHE:
        logger.warning("SCOUTNET_DEV_CACHE is on — serving Scoutnet responses from %s", CACHE_DIR)
    if settings.SCOUTNET_OVERLAY:
        _load_overlay()  # Before the first fetch, which it is laid over
    disk_cache_loaded = _load_cache_from_disk(CACHE_FILE)
    try:
        await _update_project_cache()  # Fill cache at start
    except ScoutnetRequestError:
        if disk_cache_loaded:
            logger.warning("Scoutnet unavailable at startup — serving stale disk cache")
        else:
            logger.critical("Initial cache load failed and no disk cache, shutting down")
            os._exit(1)  # Kill app without a stack trace. K8S will eventually restart it.
    if settings.SCOUTNET_SNAPSHOT_DIR:
        # Re-reading the snapshot would bring back nothing new, and would drop
        # every write made since startup from the cached records.
        logger.warning("SCOUTNET_SNAPSHOT_DIR is set — serving %s, no refreshes", settings.SCOUTNET_SNAPSHOT_DIR)
        return
    if not settings.SCOUTNET_REFRESH_INTERVAL_HOURS:
        logger.warning("SCOUTNET_REFRESH_INTERVAL_HOURS is 0 — no scheduled cache refresh")
        return
    _refresh_task = asyncio.create_task(_scheduled_cache_refresh())


async def scoutnet_shutdown() -> None:
    if _refresh_task:
        _refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await _refresh_task


def dev_cache(func):
    """Cache HTTP GET results to local files during development.

    Inactive unless SCOUTNET_DEV_CACHE is set, so it stays applied in the
    source and simply passes through in any real deployment.

    Uses a hash of the URL as filename. If cached file exists,
    returns its content instead of making the HTTP request.

    Read and write are each best-effort: production runs with a read-only
    root filesystem, and this decorator must never be why a real deployment
    fails to start. Any OSError just falls back to fetching fresh — no
    caching, but no crash either.
    """

    @functools.wraps(func)
    async def wrapper(url: str) -> dict:
        if settings.SCOUTNET_SNAPSHOT_DIR:
            # Same file naming as the dev cache, so a .dev_cache/ copied over
            # as-is works. Never falls through to Scoutnet: a live fetch is
            # exactly what snapshot mode exists to avoid.
            url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
            snapshot_file = settings.SCOUTNET_SNAPSHOT_DIR / f"{url_hash}.json"
            try:
                return json.loads(snapshot_file.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("No usable snapshot for %s: %s", url.split("?")[0], exc)
                raise ScoutnetRequestError(f"Snapshot missing: {snapshot_file}") from exc

        if not settings.SCOUTNET_DEV_CACHE:
            return await func(url)

        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_file = CACHE_DIR / f"{url_hash}.json"

        try:
            if cache_file.exists():
                logger.debug("Using cached response for %s", url)
                return json.loads(cache_file.read_text())
        except OSError as exc:
            logger.debug("Dev cache unreadable (%s), fetching fresh", exc)

        result = await func(url)

        try:
            CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(result, indent=2))
            logger.debug("Cached response for %s", url)
        except OSError as exc:
            logger.debug("Dev cache unwritable (%s), continuing without it", exc)

        return result

    return wrapper


# --- Scoutnet retrieve functions ---


@dev_cache
async def _scoutnet_get(url) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            response = await http_client.get(url)
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        url_path = url.split("?")[0]  # Strip query params (API keys)
        logger.error("Failed to fetch %s: %s: %s", url_path, type(exc).__name__, exc)
        raise ScoutnetRequestError(f"Scoutnet request failed: {url_path}") from exc


async def _get_all_projectdata_from_scoutnet() -> list[ScoutnetProjectData]:
    """
    Retrieves project data from Scoutnet for all configured projects.
    Each project's form questions are combined into one dict.

    :return: List of project data, one per configured project
    """

    async def fetch_project(project: ProjectConfig) -> ScoutnetProjectData:
        # Start questions request first - we need its response to discover form URLs
        questions_url = f"{PROJECT_API}/questions?id={project.id}&key={project.question_key}"
        questions_task = asyncio.create_task(_scoutnet_get(questions_url))

        # Start other requests in parallel
        groups_task = None
        if project.group_key:
            url = f"{PROJECT_API}/groups?flat=true&id={project.id}&key={project.group_key}"
            groups_task = asyncio.create_task(_scoutnet_get(url))

        participants_url = f"{PROJECT_API}/participants?id={project.id}&key={project.member_key}"
        participants_task = asyncio.create_task(_scoutnet_get(participants_url))

        # Wait for questions first (usually fast), then immediately start form fetches
        questions_forms = await questions_task
        forms = list(questions_forms["forms"].values())
        form_tasks = [asyncio.create_task(_scoutnet_get(f["endpoint_url"])) for f in forms]

        # Now wait for everything else in parallel
        participants = await participants_task
        groups = await groups_task if groups_task else {}
        form_results = await asyncio.gather(*form_tasks)

        questions = {"sections": {}, "questions": {}}
        for forms_data in form_results:
            questions["sections"].setdefault(forms_data["form"]["type"], {}).update(forms_data["sections"])
            questions["questions"].update(forms_data["questions"])

        return ScoutnetProjectData(
            project_id=project.id,
            project_name=project.name,
            groups=groups,
            participants=participants,
            questions=questions,
        )

    # Fetch all configured projects in parallel
    results = await asyncio.gather(*[fetch_project(p) for p in settings.SCOUTNET_PROJECTS])
    return list(results)


# --- Local functions ---


async def _update_project_cache() -> None:
    from .scoutnet_forms import scoutnet_forms_decoder

    logger.info("Start cache update")
    all_data = await _get_all_projectdata_from_scoutnet()
    if settings.SCOUTNET_OVERLAY:
        _apply_overlay(all_data[0].participants["participants"])  # One project, as get_single_project()
    scoutnet_forms_decoder(all_data, _project_cache)  # Call a project special decoder
    _save_cache_to_disk(CACHE_FILE)
    logger.info("Finish cache update")


# --- Functions called from the API handlers ---


def get_single_project() -> CachedProject:
    return next(iter(_project_cache.projects.values()))


async def set_answer(member_no: int, question_id: str, value: str) -> None:
    """Write one member's answer to one question in Scoutnet. "" clears it.

    Every write to Scoutnet goes through here, so that SCOUTNET_OVERLAY covers
    it: with the overlay on, the value is kept locally and Scoutnet is never
    called (see "Write overlay" above). Callers update their cached record
    after this returns, the same either way.

    Scoutnet checks only that the question belongs to the project, not that it
    is on the form the member registered on, so picking the right question is
    up to the caller. Only `questions` is sent: `checked_in` would force
    attended=1, which cannot be undone through this endpoint.
    """
    if settings.SCOUTNET_OVERLAY:
        _overlay_put(member_no, question_id, value)
        return
    project = get_single_project()
    config = next((p for p in settings.SCOUTNET_PROJECTS if p.id == project.project_id), None)
    if config is None or not config.update_key:
        raise ScoutnetRequestError(f"Project {project.project_id} has no update_key configured")

    body = {str(member_no): {"questions": {question_id: {"value": value}}}}
    try:
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            response = await http_client.put(
                CHECKIN_API,
                params={"id": str(project.project_id), "key": config.update_key},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ScoutnetRequestError(f"Scoutnet write failed: {type(exc).__name__}") from exc
    if response.status_code != httpx.codes.OK:
        raise ScoutnetRequestError(f"Scoutnet rejected the write: {_error_detail(response)}")

    # An entry of null means the stored value already matched, not a failure.
    entry = (response.json().get("updated_questions") or {}).get(str(member_no), {}).get(question_id)
    logger.info(
        "Question %s for member %s %s", question_id, member_no, "unchanged" if entry is None else entry.get("action")
    )


def _error_detail(response: httpx.Response) -> str:
    """The useful part of a Scoutnet error body, in either shape it comes in."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    if isinstance(body, dict):
        # Either {"error": "..."} or {"message": "...", "errors": {member: {qid: msg}}}.
        detail = body.get("error") or body.get("message") or ""
        if errors := body.get("errors"):
            detail = f"{detail} {json.dumps(errors, ensure_ascii=False)}"
        if detail:
            return f"HTTP {response.status_code}: {detail.strip()[:300]}"
    return f"HTTP {response.status_code}"


# --- API routes ---

router = APIRouter()


@router.get(
    "/refresh",
    response_model=None,
    status_code=status.HTTP_200_OK,
    response_description="All OK",
)
async def scoutnet_refresh(user: AuthUser = Depends(require_auth_user)):
    """
    Refetches all data from Scoutnet and fills cache
    """
    if settings.SCOUTNET_SNAPSHOT_DIR:
        raise HTTPException(status_code=409, detail="Serving a frozen Scoutnet snapshot; refresh is disabled")
    try:
        await _update_project_cache()
    except Exception:
        raise HTTPException(status_code=500, detail="Cache refresh failed - Scoutnet unavailable")
