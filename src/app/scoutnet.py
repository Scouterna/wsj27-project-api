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
CACHE_DIR = Path(".dev_cache")
CACHE_FILE = settings.PERSIST_DIR / "project_cache.json"


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
    group_map: dict[int, str] = field(default_factory=dict)  # A non project related map of all groups in Scoutnet


# --- Globals ---

_project_cache = ProjectCache()  # Project cache
_refresh_task: asyncio.Task | None = None  # Nightly cache refresh task


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
        _project_cache.group_map = {int(k): v for k, v in data["group_map"].items()}
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


# --- Init / shutdown ---


async def _scheduled_cache_refresh() -> None:
    while True:
        now = datetime.now(tz=ZoneInfo("Europe/Stockholm"))
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        logger.info("Running scheduled cache refresh")
        while True:  # Retry loop: keep retrying every hour until successful
            try:
                await _update_project_cache()
                break
            except Exception:
                logger.error("Cache refresh failed, will retry in 1 hour")
                await asyncio.sleep(3600)


async def scoutnet_init() -> None:
    global _refresh_task
    if settings.SCOUTNET_DEV_CACHE:
        logger.warning("SCOUTNET_DEV_CACHE is on — serving Scoutnet responses from %s", CACHE_DIR)
    await _load_initial_group_map()  # Retrive an initial group map
    disk_cache_loaded = _load_cache_from_disk(CACHE_FILE)
    try:
        await _update_project_cache()  # Fill cache at start
    except ScoutnetRequestError:
        if disk_cache_loaded:
            logger.warning("Scoutnet unavailable at startup — serving stale disk cache")
        else:
            logger.critical("Initial cache load failed and no disk cache, shutting down")
            os._exit(1)  # Kill app without a stack trace. K8S will eventually restart it.
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
    scoutnet_forms_decoder(all_data, _project_cache)  # Call a project special decoder
    _save_cache_to_disk(CACHE_FILE)
    logger.info("Finish cache update")


async def _load_initial_group_map() -> None:
    group_map = {}
    if settings.SCOUTNET_BODYLIST_KEY:  # Fetch map from Scoutnet
        try:
            url = f"https://scoutnet.se/api/body_key_list?id={settings.SCOUTNET_BODYLIST_ID}&key={settings.SCOUTNET_BODYLIST_KEY}"
            raw_map = await _scoutnet_get(url)
            group_map = {g["body_id"]: g["body_name"] for g in raw_map.values() if g.get("body_type") == "group"}
        except Exception:
            logger.warning("Failed to fetch group_map from Scoutnet, falling back to local file")
    if not group_map:
        try:  # Fall back to persisted disk cache
            data = json.loads(CACHE_FILE.read_text())
            group_map = {int(k): v for k, v in data["group_map"].items()}
            logger.info("Loaded group_map from disk cache")
        except Exception:
            logger.warning("Failed to load group_map from disk cache, using empty initial map")

    _project_cache.group_map = group_map
    logger.info("Loaded group_map with %d entries", len(_project_cache.group_map))


# --- Functions called from the API handlers ---


def get_single_project() -> CachedProject:
    return next(iter(_project_cache.projects.values()))


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
    try:
        await _update_project_cache()
    except Exception:
        raise HTTPException(status_code=500, detail="Cache refresh failed - Scoutnet unavailable")
