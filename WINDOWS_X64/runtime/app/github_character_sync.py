from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_JSON_FILES = 60
MAX_CHARACTERS = 200


class GitHubSyncError(ValueError):
    pass


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "THERE-character-sync",
    }
    token = (os.environ.get("THERE_GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _download(url: str, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    request = Request(url, headers=_headers(), method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            data = response.read(max_bytes + 1)
    except HTTPError as exc:
        if exc.code == 404:
            raise GitHubSyncError("GitHub источник не найден или недоступен") from exc
        if exc.code == 403:
            raise GitHubSyncError("GitHub временно ограничил запросы. Попробуй позже") from exc
        raise GitHubSyncError(f"GitHub ответил кодом {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GitHubSyncError("Не удалось подключиться к GitHub") from exc
    if len(data) > max_bytes:
        raise GitHubSyncError("Файл на GitHub слишком большой")
    return data


def _download_json(url: str, *, max_bytes: int = MAX_RESPONSE_BYTES):
    try:
        return json.loads(_download(url, max_bytes=max_bytes).decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise GitHubSyncError("JSON на GitHub должен быть в UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise GitHubSyncError("GitHub файл содержит некорректный JSON") from exc


def _source_parts(raw_url: str) -> dict[str, str]:
    text = (raw_url or "").strip()
    if not text:
        raise GitHubSyncError("Вставь ссылку GitHub")
    if "://" not in text:
        text = "https://" + text
    try:
        uri = urlparse(text)
    except Exception as exc:
        raise GitHubSyncError("Некорректная ссылка GitHub") from exc

    host = (uri.hostname or "").lower()
    parts = [part for part in uri.path.split("/") if part]

    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise GitHubSyncError("Некорректная raw ссылка GitHub")
        return {
            "mode": "file",
            "owner": parts[0],
            "repo": parts[1].removesuffix(".git"),
            "branch": parts[2],
            "path": "/".join(parts[3:]),
            "original": text,
        }

    if host != "github.com" or len(parts) < 2:
        raise GitHubSyncError("Разрешены только ссылки github.com")

    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    branch = ""
    path = ""
    mode = "repo"
    if len(parts) >= 5 and parts[2] == "blob":
        mode = "file"
        branch = parts[3]
        path = "/".join(parts[4:])
    elif len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
        path = "/".join(parts[4:])

    return {
        "mode": mode,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "path": path.strip("/"),
        "original": text,
    }


def _api(parts: dict[str, str], suffix: str) -> str:
    owner = quote(parts["owner"], safe="")
    repo = quote(parts["repo"], safe="")
    return f"https://api.github.com/repos/{owner}/{repo}{suffix}"


def _raw(parts: dict[str, str], branch: str, path: str) -> str:
    owner = quote(parts["owner"], safe="")
    repo = quote(parts["repo"], safe="")
    branch_q = quote(branch, safe="")
    path_q = "/".join(quote(part, safe="") for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch_q}/{path_q}"


def _looks_like_character(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    markers = {
        "hp", "stats", "classes", "race", "skills", "equipment",
        "personality", "general_info", "spell_slots", "ac",
    }
    hits = sum(1 for key in markers if key in obj)
    return hits >= 2 or ("name" in obj and hits >= 1)


def _character_key(source_path: str, inner_key: str) -> str:
    raw = f"{source_path}\n{inner_key}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:24]


def _extract_characters(payload, source_path: str) -> list[dict]:
    out: list[dict] = []

    def add(obj: dict, inner_key: str, fallback_name: str):
        if len(out) >= MAX_CHARACTERS or not _looks_like_character(obj):
            return
        data = dict(obj)
        name = str(data.get("name") or fallback_name or inner_key or "Персонаж").strip()
        if not name:
            name = "Персонаж"
        out.append({
            "key": _character_key(source_path, inner_key),
            "name": name[:120],
            "source_path": source_path,
            "data": data,
        })

    if isinstance(payload, dict):
        nested = payload.get("characters")
        if isinstance(nested, (dict, list)):
            out.extend(_extract_characters(nested, source_path))
            return out[:MAX_CHARACTERS]
        if _looks_like_character(payload):
            add(payload, "$", PurePosixPath(source_path).stem)
            return out
        for key, value in list(payload.items())[:MAX_CHARACTERS]:
            if isinstance(value, dict):
                add(value, str(key), str(key))
    elif isinstance(payload, list):
        for index, value in enumerate(payload[:MAX_CHARACTERS]):
            if isinstance(value, dict):
                add(value, str(index), f"Персонаж {index + 1}")
    return out[:MAX_CHARACTERS]


def _candidate_score(path: str) -> tuple[int, str]:
    low = path.lower()
    name = PurePosixPath(path).name.lower()
    if name == "characters.json":
        return (0, low)
    if name == "character.json":
        return (1, low)
    if name.endswith("_character.json") or name.endswith("-character.json"):
        return (2, low)
    segments = {segment.lower() for segment in PurePosixPath(path).parts}
    if {"characters", "character", "players", "персонажи"} & segments:
        return (3, low)
    return (10, low)


def _repo_json_files(parts: dict[str, str]) -> tuple[str, list[dict]]:
    meta = _download_json(_api(parts, ""))
    branch = parts["branch"] or str(meta.get("default_branch") or "main")
    branch_data = _download_json(_api(parts, f"/branches/{quote(branch, safe='')}"))
    tree_sha = (((branch_data or {}).get("commit") or {}).get("commit") or {}).get("tree", {}).get("sha")
    if not tree_sha:
        raise GitHubSyncError("Не удалось прочитать ветку GitHub")
    tree = _download_json(_api(parts, f"/git/trees/{tree_sha}?recursive=1"))
    if tree.get("truncated"):
        raise GitHubSyncError("Репозиторий слишком большой для автоматического поиска")

    base = parts["path"].strip("/")
    found = []
    for entry in tree.get("tree") or []:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "")
        size = int(entry.get("size") or 0)
        if not path.lower().endswith(".json") or size > MAX_FILE_BYTES:
            continue
        if base and path != base and not path.startswith(base + "/"):
            continue
        found.append({"path": path, "size": size, "score": _candidate_score(path)})

    found.sort(key=lambda item: item["score"])
    return branch, found[:MAX_JSON_FILES]


def load_github_characters(raw_url: str) -> list[dict]:
    parts = _source_parts(raw_url)
    collected: list[dict] = []

    if parts["mode"] == "file":
        if not parts["path"].lower().endswith(".json"):
            raise GitHubSyncError("Ссылка должна вести на JSON с персонажем или на репозиторий")
        payload = _download_json(_raw(parts, parts["branch"], parts["path"]), max_bytes=MAX_FILE_BYTES)
        collected.extend(_extract_characters(payload, parts["path"]))
    else:
        branch, files = _repo_json_files(parts)
        if not files:
            raise GitHubSyncError("В репозитории не найден JSON с персонажами")
        for item in files:
            try:
                payload = _download_json(_raw(parts, branch, item["path"]), max_bytes=MAX_FILE_BYTES)
            except GitHubSyncError:
                continue
            collected.extend(_extract_characters(payload, item["path"]))
            if len(collected) >= MAX_CHARACTERS:
                break

    deduped = {}
    for item in collected:
        deduped[item["key"]] = item
    result = list(deduped.values())[:MAX_CHARACTERS]
    if not result:
        raise GitHubSyncError("В GitHub источнике не найдено данных персонажей")
    result.sort(key=lambda item: item["name"].casefold())
    return result


def character_summaries(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        data = item.get("data") or {}
        hp = data.get("hp") if isinstance(data.get("hp"), dict) else {}
        result.append({
            "key": item["key"],
            "name": item["name"],
            "source_path": item["source_path"],
            "race": str(data.get("race") or "")[:100],
            "level": data.get("level"),
            "hp_current": hp.get("current"),
            "hp_max": hp.get("max"),
        })
    return result
