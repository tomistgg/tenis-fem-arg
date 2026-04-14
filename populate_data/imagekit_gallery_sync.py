import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests


DEFAULT_API_URL = "https://api.imagekit.io/v1/files"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".heic", ".heif"}


def _normalize_root(root: Optional[str]) -> str:
    value = (root or "/").strip()
    if not value or value == "/":
        return "/"
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/")


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _is_image_file(item: Dict[str, Any]) -> bool:
    item_type = str(item.get("type") or "").strip().lower()
    if item_type and item_type != "file":
        return False

    file_path = str(item.get("filePath") or item.get("path") or item.get("name") or "").strip()
    ext = os.path.splitext(file_path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return True

    mime = str(item.get("mime") or item.get("mimeType") or "").strip().lower()
    return mime.startswith("image/")


def _relative_path_from_root(file_path: str, root: str) -> str:
    norm = "/" + str(file_path or "").lstrip("/")
    if root == "/":
        return norm.lstrip("/")
    prefix = root + "/"
    if norm.startswith(prefix):
        return norm[len(prefix):]
    return ""


def _split_tournament_and_public_id(relative_path: str) -> Tuple[str, str]:
    rel = str(relative_path or "").strip().lstrip("/")
    if not rel:
        return "Unsorted", ""
    parts = rel.split("/")
    if len(parts) == 1:
        return "Unsorted", rel
    return parts[0], rel


def _slug_to_player_name(slug: str) -> str:
    bits = [b for b in str(slug or "").strip("-").split("-") if b]
    if not bits:
        return ""
    return " ".join(token[:1].upper() + token[1:] for token in bits)


def _infer_players_from_public_id(public_id: str) -> List[str]:
    base = os.path.basename(str(public_id or "").strip())
    stem, _ = os.path.splitext(base)
    if not stem:
        return []

    # Common filename shape:
    # marina-bassols-ribera-y-andrea-lazaro-garcia_54487464866_o
    stem = re.split(r"_\d", stem, maxsplit=1)[0]
    if not stem:
        return []

    separators = ["-y-", "-and-", "_y_", "_and_"]
    parts = [stem]
    for sep in separators:
        if sep in stem:
            parts = [p for p in stem.split(sep) if p]
            break

    players: List[str] = []
    for part in parts:
        name = _slug_to_player_name(part.replace("_", "-"))
        if name and name not in players:
            players.append(name)
    return players


def _coerce_players(value: Any) -> List[str]:
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "|" in text:
            return [x.strip() for x in text.split("|") if x.strip()]
        if "," in text:
            return [x.strip() for x in text.split(",") if x.strip()]
        return [text]
    return []


def _metadata_players(item: Dict[str, Any]) -> List[str]:
    custom = item.get("customMetadata")
    if not isinstance(custom, dict):
        custom = {}
    for key in ("players", "player_names", "player", "athletes"):
        if key in custom:
            parsed = _coerce_players(custom.get(key))
            if parsed:
                return parsed
    return []


def _metadata_tournament(item: Dict[str, Any]) -> str:
    custom = item.get("customMetadata")
    if not isinstance(custom, dict):
        custom = {}
    for key in ("tournament", "album", "event"):
        text = str(custom.get(key) or "").strip()
        if text:
            return text
    return ""


def _metadata_is_cover(item: Dict[str, Any]) -> Optional[bool]:
    custom = item.get("customMetadata")
    if not isinstance(custom, dict):
        custom = {}
    for key in ("is_cover", "isCover", "cover", "album_cover"):
        if key in custom:
            parsed = _to_bool(custom.get(key))
            if parsed is not None:
                return parsed

    tags = item.get("tags")
    if isinstance(tags, list):
        lowered = {str(t).strip().lower() for t in tags if str(t).strip()}
        if "cover" in lowered or "album-cover" in lowered:
            return True
    return None


def _load_existing_map(gallery_json_path: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if not os.path.exists(gallery_json_path):
        return {}
    try:
        with open(gallery_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}

    mapped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        tournament = str(row.get("tournament") or row.get("album") or "").strip()
        pid = str(row.get("public_id") or row.get("path") or "").strip().lstrip("/")
        if not pid:
            continue
        base = os.path.basename(pid)
        mapped[(tournament, pid)] = row
        mapped[(tournament, base)] = row
        mapped[("", pid)] = row
        mapped[("", base)] = row
    return mapped


def _fetch_all_files(api_url: str, private_key: str, timeout: int = 30) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    limit = 100
    skip = 0
    auth = (private_key, "")

    while True:
        params = {"limit": limit, "skip": skip}
        response = requests.get(api_url, params=params, auth=auth, timeout=timeout)
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, list):
            page = payload
        elif isinstance(payload, dict):
            # Defensive handling for potential wrapper objects
            if isinstance(payload.get("files"), list):
                page = payload["files"]
            elif isinstance(payload.get("items"), list):
                page = payload["items"]
            elif isinstance(payload.get("data"), list):
                page = payload["data"]
            else:
                page = []
        else:
            page = []

        if not page:
            break

        files.extend(x for x in page if isinstance(x, dict))
        skip += len(page)
        if len(page) < limit:
            break

    return files


def sync_gallery_manifest(
    gallery_json_path: str,
    private_key: Optional[str] = None,
    root_folder: Optional[str] = None,
    api_url: Optional[str] = None,
) -> Dict[str, Any]:
    key = (private_key or os.getenv("IMAGEKIT_PRIVATE_KEY") or "").strip()
    if not key:
        return {"status": "skipped", "reason": "missing_private_key"}

    root = _normalize_root(root_folder or os.getenv("IMAGEKIT_GALLERY_ROOT") or "/")
    endpoint = (api_url or os.getenv("IMAGEKIT_API_URL") or DEFAULT_API_URL).strip()

    existing_map = _load_existing_map(gallery_json_path)
    fetched_files = _fetch_all_files(endpoint, key)
    photos: List[Dict[str, Any]] = []

    for item in fetched_files:
        if not _is_image_file(item):
            continue
        file_path = str(item.get("filePath") or item.get("path") or "").strip()
        rel = _relative_path_from_root(file_path, root)
        if not rel:
            continue

        inferred_tournament, rel_public_id = _split_tournament_and_public_id(rel)
        if not rel_public_id:
            continue
        basename = os.path.basename(rel_public_id)

        existing = (
            existing_map.get((inferred_tournament, rel_public_id))
            or existing_map.get((inferred_tournament, basename))
            or existing_map.get(("", rel_public_id))
            or existing_map.get(("", basename))
            or {}
        )
        if not isinstance(existing, dict):
            existing = {}

        tournament = (
            _metadata_tournament(item)
            or str(existing.get("tournament") or existing.get("album") or "").strip()
            or inferred_tournament
            or "Unsorted"
        )

        players = _metadata_players(item)
        if not players:
            players = _coerce_players(existing.get("players"))
        if not players:
            players = _infer_players_from_public_id(rel_public_id)

        cover_meta = _metadata_is_cover(item)
        if cover_meta is None:
            cover_meta = _to_bool(existing.get("is_cover"))

        row: Dict[str, Any] = {
            "public_id": rel_public_id,
            "players": players,
            "tournament": tournament,
        }
        if cover_meta is True:
            row["is_cover"] = True

        # Preserve optional informational fields if present in old manifest.
        for optional_key in ("caption", "date"):
            if optional_key in existing and str(existing.get(optional_key) or "").strip():
                row[optional_key] = existing.get(optional_key)

        created_at = str(item.get("createdAt") or item.get("updatedAt") or "")
        row["_sort_date"] = created_at
        photos.append(row)

    photos.sort(
        key=lambda p: (
            str(p.get("_sort_date") or ""),
            str(p.get("tournament") or "").lower(),
            str(p.get("public_id") or "").lower(),
        )
    )
    for p in photos:
        p.pop("_sort_date", None)

    os.makedirs(os.path.dirname(gallery_json_path), exist_ok=True)
    with open(gallery_json_path, "w", encoding="utf-8") as f:
        json.dump(photos, f, ensure_ascii=False, separators=(",", ":"))

    return {
        "status": "updated",
        "count": len(photos),
        "root": root,
        "endpoint": endpoint,
    }

