import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, TypedDict
from urllib.parse import urlparse

import aiohttp

from app.events import get_subscribed_events


@dataclass
class HookError:
    code: str
    message: str
    detail: Optional[str] = None


class OrgSummary(TypedDict):
    login: str
    is_personal: bool
    source: str
    installation_id: int


class RepoSummary(TypedDict):
    full_name: str
    name: str
    owner: str
    private: bool
    stars: int
    description: str
    permissions_admin: bool
    source: str
    installation_id: int
    html_url: str


_TTL = 300.0
_cache: dict[tuple, tuple[object, float]] = {}


def _get_cached(key: tuple) -> object | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at <= time.monotonic():
        _cache.pop(key, None)
        return None
    return value


def _set_cached(key: tuple, value: object) -> None:
    _cache[key] = (value, time.monotonic() + _TTL)


def invalidate(token: str) -> None:
    for key in [k for k in list(_cache.keys()) if k and k[0] == token]:
        _cache.pop(key, None)


async def invalidate_for_user(user, _config=None) -> None:
    if user is not None and user.token:
        invalidate(user.token)


def normalize_server_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        return "https://codeberg.org"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path.split("/")[0]
    base = f"{scheme}://{netloc}".rstrip("/")
    return base


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Telegram-Notifier-Bot",
    }


def _hook_url(host: str, endpoint: str) -> str:
    host_clean = host.strip()
    if not host_clean.startswith("http://") and not host_clean.startswith("https://"):
        if "localhost" in host_clean or "127.0.0.1" in host_clean:
            host_clean = "http://" + host_clean
        else:
            host_clean = "https://" + host_clean
    if not host_clean.endswith("/"):
        host_clean += "/"
    return f"{host_clean}webhook/{endpoint}"


async def validate_server(server_url: str) -> tuple[bool, str]:
    base_url = normalize_server_url(server_url)
    version_url = f"{base_url}/api/v1/version"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(version_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    version = data.get("version", "Forgejo/Gitea")
                    return True, version
                return False, f"Server responded with HTTP {resp.status}"
    except Exception as e:
        return False, f"Connection failed: {str(e)[:100]}"


async def validate_token(server_url: str, token: str) -> tuple[bool, dict | str]:
    base_url = normalize_server_url(server_url)
    user_url = f"{base_url}/api/v1/user"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(user_url, headers=_auth_headers(token)) as resp:
                if resp.status == 200:
                    user_data = await resp.json()
                    return True, user_data
                if resp.status == 401 or resp.status == 403:
                    return False, "Token is invalid, expired, or missing permissions."
                return False, f"Server returned HTTP {resp.status}"
    except Exception as e:
        return False, f"Connection error: {str(e)[:120]}"


async def list_orgs_for_user(user, _config=None) -> list[OrgSummary]:
    if user is None or not user.token:
        return []

    server_url = normalize_server_url(user.server_url or "https://codeberg.org")
    token = user.token
    key = (token, server_url, "orgs")
    cached = _get_cached(key)
    if cached is not None:
        return cached

    result: list[OrgSummary] = []
    seen: set[str] = set()

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f"{server_url}/api/v1/user", headers=_auth_headers(token)) as resp:
                if resp.status == 200:
                    me = await resp.json()
                    login = me.get("username") or me.get("login") or ""
                    if login:
                        seen.add(login)
                        result.append(
                            OrgSummary(
                                login=login,
                                is_personal=True,
                                source="pat",
                                installation_id=0,
                            )
                        )

            async with session.get(
                f"{server_url}/api/v1/user/orgs?limit=50",
                headers=_auth_headers(token),
            ) as resp:
                if resp.status == 200:
                    orgs = await resp.json()
                    if isinstance(orgs, list):
                        for org in orgs:
                            org_name = org.get("username") or org.get("name") or ""
                            if org_name and org_name not in seen:
                                seen.add(org_name)
                                result.append(
                                    OrgSummary(
                                        login=org_name,
                                        is_personal=False,
                                        source="pat",
                                        installation_id=0,
                                    )
                                )
    except Exception as e:
        logging.warning("Error fetching orgs from %s: %s", server_url, e)

    _set_cached(key, result)
    return result


async def list_repos_for_org(user, org: OrgSummary, _config=None) -> list[RepoSummary]:
    if user is None or not user.token:
        return []

    server_url = normalize_server_url(user.server_url or "https://codeberg.org")
    token = user.token
    key = (token, server_url, "repos", org["login"], org["is_personal"])
    cached = _get_cached(key)
    if cached is not None:
        return cached

    endpoint = (
        f"{server_url}/api/v1/user/repos?limit=50"
        if org["is_personal"]
        else f"{server_url}/api/v1/orgs/{org['login']}/repos?limit=50"
    )

    result: list[RepoSummary] = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            page = 1
            while page <= 5:
                url = f"{endpoint}&page={page}"
                async with session.get(url, headers=_auth_headers(token)) as resp:
                    if resp.status != 200:
                        break
                    repos = await resp.json()
                    if not isinstance(repos, list) or not repos:
                        break
                    for r in repos:
                        full_name = r.get("full_name") or f"{org['login']}/{r.get('name')}"
                        perms = r.get("permissions") or {}
                        admin = bool(perms.get("admin", True))
                        owner = (r.get("owner") or {}).get("username") or org["login"]
                        result.append(
                            RepoSummary(
                                full_name=full_name,
                                name=r.get("name", ""),
                                owner=owner,
                                private=bool(r.get("private", False)),
                                stars=int(r.get("stars_count", 0)),
                                description=(r.get("description") or "")[:200],
                                permissions_admin=admin,
                                source="pat",
                                installation_id=0,
                                html_url=r.get("html_url") or f"{server_url}/{full_name}",
                            )
                        )
                    if len(repos) < 50:
                        break
                    page += 1
    except Exception as e:
        logging.warning("Error fetching repos from %s: %s", server_url, e)

    result.sort(
        key=lambda x: (not x["permissions_admin"], -x["stars"], x["full_name"])
    )
    _set_cached(key, result)
    return result


async def check_repo(
    server_url: str, token: str, repo_name: str
) -> tuple[bool, dict | HookError]:
    base_url = normalize_server_url(server_url)
    url = f"{base_url}/api/v1/repos/{repo_name}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, headers=_auth_headers(token)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return True, data
                if resp.status == 404:
                    return False, HookError(
                        "not_found",
                        f"Repository <code>{repo_name}</code> not found on {base_url}.",
                    )
                if resp.status == 401 or resp.status == 403:
                    return False, HookError(
                        "no_permission",
                        f"Access denied to <code>{repo_name}</code>. Check token permissions.",
                    )
                return False, HookError("unknown", f"API error ({resp.status})")
    except Exception as e:
        return False, HookError("server_unreachable", f"Connection error: {e}")


async def create_webhook(
    server_url: str,
    token: str,
    repo_name: str,
    host: str,
    endpoint: str,
) -> Optional[HookError]:
    base_url = normalize_server_url(server_url)
    target_url = _hook_url(host, endpoint)
    events = list(get_subscribed_events())

    hooks_url = f"{base_url}/api/v1/repos/{repo_name}/hooks"
    payload = {
        "type": "forgejo",
        "config": {
            "url": target_url,
            "content_type": "json",
        },
        "events": events,
        "active": True,
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.post(
                hooks_url, headers=_auth_headers(token), json=payload
            ) as resp:
                if resp.status in (200, 201):
                    return None
                if resp.status in (400, 422):
                    payload["type"] = "gitea"
                    async with session.post(
                        hooks_url, headers=_auth_headers(token), json=payload
                    ) as resp2:
                        if resp2.status in (200, 201):
                            return None
                        text = await resp2.text()
                        return HookError(
                            "error",
                            f"Couldn't create webhook ({resp2.status}): {text[:150]}",
                        )
                if resp.status == 403:
                    return HookError(
                        "no_permission",
                        "Admin permissions required to create webhooks on this repository.",
                    )
                text = await resp.text()
                return HookError(
                    "unknown", f"Failed to create webhook ({resp.status}): {text[:150]}"
                )
    except Exception as e:
        return HookError("server_unreachable", f"Connection error: {e}")


async def update_webhook(
    server_url: str,
    token: str,
    repo_name: str,
    host: str,
    endpoint: str,
) -> Optional[HookError]:
    base_url = normalize_server_url(server_url)
    target_url = _hook_url(host, endpoint)
    events = list(get_subscribed_events())
    hooks_url = f"{base_url}/api/v1/repos/{repo_name}/hooks"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.get(hooks_url, headers=_auth_headers(token)) as resp:
                if resp.status != 200:
                    return await create_webhook(
                        server_url, token, repo_name, host, endpoint
                    )
                hooks = await resp.json()

            existing_id = None
            if isinstance(hooks, list):
                for hook in hooks:
                    config = hook.get("config") or {}
                    if config.get("url") == target_url:
                        existing_id = hook.get("id")
                        break

            if existing_id is not None:
                patch_url = f"{hooks_url}/{existing_id}"
                patch_payload = {
                    "config": {
                        "url": target_url,
                        "content_type": "json",
                    },
                    "events": events,
                    "active": True,
                }
                async with session.patch(
                    patch_url, headers=_auth_headers(token), json=patch_payload
                ) as patch_resp:
                    if patch_resp.status in (200, 201):
                        return None
                    text = await patch_resp.text()
                    return HookError("error", f"Failed to update hook: {text[:150]}")
            else:
                return await create_webhook(
                    server_url, token, repo_name, host, endpoint
                )
    except Exception as e:
        return HookError("server_unreachable", f"Connection error: {e}")


async def get_subscribed_events_for(
    server_url: str, token: str, repo_name: str, host: str, endpoint: str
) -> set[str] | HookError:
    base_url = normalize_server_url(server_url)
    target_url = _hook_url(host, endpoint)
    hooks_url = f"{base_url}/api/v1/repos/{repo_name}/hooks"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(hooks_url, headers=_auth_headers(token)) as resp:
                if resp.status != 200:
                    return HookError(
                        "not_found", f"Could not list webhooks on {repo_name} ({resp.status})"
                    )
                hooks = await resp.json()
                if isinstance(hooks, list):
                    for hook in hooks:
                        config = hook.get("config") or {}
                        if config.get("url") == target_url:
                            return set(hook.get("events") or [])
                return HookError(
                    "not_found",
                    f"No webhook with URL {target_url} found on <code>{repo_name}</code>.",
                )
    except Exception as e:
        return HookError("server_unreachable", f"Connection error: {e}")
