"""Durable saved projects and repo sets.

These lists used to live only inside ``runtime_settings.json``. A later
settings write that could not read that file replaced it and dropped them.
``saved_catalog.json`` next to that file is the copy the dashboard reads.
A save of any other setting does not rewrite it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from src.logger import logger

_PROJECTS = "project_repositories"
_SETS = "repository_sets"
_KEYS = (_PROJECTS, _SETS)


def saved_catalog_path() -> Path:
    """``{data_dir}/saved_catalog.json``, beside ``runtime_settings.json``."""
    from src.config import runtime_settings_path

    return runtime_settings_path().parent / "saved_catalog.json"


def read_saved_catalog() -> Optional[Dict[str, Any]]:
    """Parsed catalog, or None when the file is missing or unreadable.

    Unreadable is not the same as an empty list. Callers must not replace
    the file in that case.
    """
    path = saved_catalog_path()
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning(f"Could not read saved catalog {path}: {exc}")
        return None
    if not isinstance(data, dict):
        logger.warning(f"Saved catalog {path} is not an object")
        return None
    return data


def catalog_file_exists() -> bool:
    return saved_catalog_path().is_file()


def _lists_from_raw(raw_projects: Any, raw_sets: Any) -> Dict[str, Any]:
    from src.dashboard.project_repos import parse_project_repositories
    from src.dashboard.repo_sets import parse_repository_sets

    return {
        _PROJECTS: parse_project_repositories(raw_projects),
        _SETS: parse_repository_sets(raw_sets),
    }


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_saved_catalog(projects: Any = None, sets: Any = None) -> None:
    """Merge the given lists into the catalog. Omitted arguments stay as stored.

    A list argument replaces that side, including an explicit empty list.
    """
    from src.dashboard.project_repos import parse_project_repositories
    from src.dashboard.repo_sets import parse_repository_sets

    if projects is None and sets is None:
        return
    path = saved_catalog_path()
    if path.is_file():
        current = read_saved_catalog()
        if current is None:
            logger.error(
                f"Saved catalog {path} is unreadable; left the file unchanged"
            )
            return
    else:
        current = {}
    if projects is not None:
        current[_PROJECTS] = parse_project_repositories(projects)
    if sets is not None:
        current[_SETS] = parse_repository_sets(sets)
    current.setdefault(_PROJECTS, [])
    current.setdefault(_SETS, [])
    _atomic_write(path, current)
    logger.info(
        "Saved catalog "
        f"{path}: {len(current[_PROJECTS])} projects, "
        f"{len(current[_SETS])} repo sets"
    )


def persist_catalog_updates(updates: Dict[str, Any]) -> None:
    """Write catalog fields present in a runtime-settings update."""
    if _PROJECTS not in updates and _SETS not in updates:
        return
    write_saved_catalog(
        projects=updates[_PROJECTS] if _PROJECTS in updates else None,
        sets=updates[_SETS] if _SETS in updates else None,
    )


def apply_saved_catalog(settings_obj: Any) -> None:
    """Catalog wins over ``runtime_settings.json``. Seed the file once when missing."""
    from src.dashboard.project_repos import project_repositories_to_json
    from src.dashboard.repo_sets import repository_sets_to_json

    path = saved_catalog_path()
    if not path.is_file():
        _seed_catalog(settings_obj)
        return
    data = read_saved_catalog()
    if data is None:
        return
    if _PROJECTS in data:
        encoded = project_repositories_to_json(data.get(_PROJECTS))
        try:
            setattr(settings_obj, _PROJECTS, encoded)
        except Exception as exc:
            logger.warning(f"Could not apply saved projects: {exc}")
    if _SETS in data:
        encoded_sets = repository_sets_to_json(data.get(_SETS))
        try:
            setattr(settings_obj, _SETS, encoded_sets)
        except Exception as exc:
            logger.warning(f"Could not apply repo sets: {exc}")


def _seed_catalog(settings_obj: Any) -> None:
    """Copy the runtime lists into the catalog. Skip a value that does not parse."""
    raw_projects = getattr(settings_obj, _PROJECTS, "") or ""
    raw_sets = getattr(settings_obj, _SETS, "") or ""
    parsed = _lists_from_raw(raw_projects, raw_sets)
    projects_text = str(raw_projects).strip()
    sets_text = str(raw_sets).strip()
    if not parsed[_PROJECTS] and projects_text not in ("", "[]"):
        logger.warning("Saved catalog not seeded; project list did not parse")
        return
    if not parsed[_SETS] and sets_text not in ("", "[]"):
        logger.warning("Saved catalog not seeded; repo sets did not parse")
        return
    if not parsed[_PROJECTS] and not parsed[_SETS]:
        return
    write_saved_catalog(projects=parsed[_PROJECTS], sets=parsed[_SETS])
