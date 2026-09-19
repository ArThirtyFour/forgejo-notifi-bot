import asyncio
import logging
from typing import Any, Optional

import aiohttp
from aiogram import Bot

from app.config import Config
from app.db.functions import EventSetting, Integration, User
from app.events import EventCtx, build_message
from app.utils.forgejo import normalize_server_url
from app.webhook.api import send_message

POLL_INTERVAL = 15.0


_known_branches: dict[int, dict[str, str]] = {}
_known_pulls: dict[int, dict[int, tuple[str, bool]]] = {}
_known_issues: dict[int, dict[int, str]] = {}


async def _send_push_notification(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
    branch_name: str,
    commits: list[dict],
    prev_sha: Optional[str],
) -> None:
    repo_name = integration.repository_name
    chat = integration.chat
    if not commits or not repo_name or not chat:
        return

    latest_sha = commits[0].get("sha")
    if not latest_sha:
        return

    new_commits = []
    found_previous = False
    if prev_sha:
        for c in commits:
            if c.get("sha") == prev_sha:
                found_previous = True
                break
            new_commits.append(c)

    if not found_previous:
        new_commits = [commits[0]]

    if not new_commits:
        return

    logging.info(
        "Poller detected %d new commit(s) for %s on %s",
        len(new_commits),
        repo_name,
        branch_name,
    )

    if not await EventSetting.is_enabled(chat.chat_id, "push"):
        return

    commits_payload = []
    for c in new_commits:
        comm_author = (c.get("commit") or {}).get("author") or {}
        author_obj = c.get("author") or {}
        author_name = (
            comm_author.get("name")
            or author_obj.get("username")
            or author_obj.get("login")
            or "user"
        )
        username = (
            author_obj.get("username")
            or author_obj.get("login")
            or author_name
        )
        author_html_url = (
            author_obj.get("html_url") or f"{server_url}/{username}"
        )

        stats = c.get("stats") or {}
        additions = stats.get("additions")
        deletions = stats.get("deletions")

        commits_payload.append(
            {
                "id": c["sha"],
                "message": (c.get("commit") or {}).get("message") or "",
                "url": (
                    c.get("html_url")
                    or f"{server_url}/{repo_name}/commit/{c['sha']}"
                ),
                "author": {
                    "name": author_name,
                    "email": comm_author.get("email"),
                    "username": username,
                    "html_url": author_html_url,
                },
                "added": [
                    f["filename"]
                    for f in (c.get("files") or [])
                    if f.get("status") == "added"
                ],
                "removed": [
                    f["filename"]
                    for f in (c.get("files") or [])
                    if f.get("status") == "deleted"
                ],
                "modified": [
                    f["filename"]
                    for f in (c.get("files") or [])
                    if f.get("status") == "modified"
                ],
                "additions": additions,
                "deletions": deletions,
            }
        )

    top_author = new_commits[0].get("author") or {}
    sender_login = (
        top_author.get("username")
        or top_author.get("login")
        or commits_payload[0]["author"]["name"]
    )
    sender_html_url = (
        top_author.get("html_url") or f"{server_url}/{sender_login}"
    )

    if found_previous and prev_sha:
        compare_link = f"{server_url}/{repo_name}/compare/{prev_sha}...{latest_sha}"
    else:
        parents = new_commits[0].get("parents") or []
        if parents and parents[0].get("sha"):
            compare_link = f"{server_url}/{repo_name}/compare/{parents[0]['sha']}...{latest_sha}"
        else:
            compare_link = (
                new_commits[0].get("html_url")
                or f"{server_url}/{repo_name}/commit/{latest_sha}"
            )

    payload = {
        "ref": f"refs/heads/{branch_name}",
        "compare": compare_link,
        "commits": commits_payload,
        "repository": {
            "full_name": repo_name,
            "html_url": f"{server_url}/{repo_name}",
            "private": False,
        },
        "sender": {
            "login": sender_login,
            "username": sender_login,
            "html_url": sender_html_url,
        },
    }

    ctx = EventCtx(auth_token=token, server_url=server_url, config=config)
    msg = build_message("push", payload, ctx)
    if msg:
        await send_message(session, integration, msg)


async def _poll_commits(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
) -> None:
    repo_name = integration.repository_name
    if not repo_name:
        return
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Notifier-Poller",
    }

    branches_url = f"{server_url}/api/v1/repos/{repo_name}/branches"
    branches = None
    try:
        async with session.get(
            branches_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                branches = await resp.json()
    except Exception as e:
        logging.debug("Poller branches error for %s: %s", repo_name, e)

    if isinstance(branches, list) and branches:
        current_branch_map: dict[str, str] = {}
        for b in branches:
            name = b.get("name")
            commit_obj = b.get("commit") or {}
            sha = commit_obj.get("id")
            if name and sha:
                current_branch_map[name] = sha

        if integration.id not in _known_branches:
            if integration.last_commit_sha is None:
                _known_branches[integration.id] = current_branch_map
                if branches:
                    default_sha = (branches[0].get("commit") or {}).get("id")
                    if default_sha:
                        integration.last_commit_sha = default_sha
                        await integration.save()
                return
            else:
                _known_branches[integration.id] = {
                    b_name: (
                        integration.last_commit_sha
                        if b_name in ("main", "master")
                        else b_sha
                    )
                    for b_name, b_sha in current_branch_map.items()
                }

        prev_map = _known_branches[integration.id]

        for b_name, b_sha in current_branch_map.items():
            prev_sha = prev_map.get(b_name)
            if prev_sha == b_sha:
                continue

            commits_url = f"{server_url}/api/v1/repos/{repo_name}/commits?sha={b_name}&limit=10"
            commits = None
            try:
                async with session.get(
                    commits_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        commits = await resp.json()
            except Exception as e:
                logging.debug(
                    "Poller commits error for %s (%s): %s",
                    repo_name,
                    b_name,
                    e,
                )

            if isinstance(commits, list) and commits:
                await _send_push_notification(
                    session,
                    integration,
                    server_url,
                    token,
                    config,
                    b_name,
                    commits,
                    prev_sha,
                )

        _known_branches[integration.id] = current_branch_map
        if branches:
            latest_main = (
                current_branch_map.get("main")
                or current_branch_map.get("master")
                or list(current_branch_map.values())[0]
            )
            if latest_main and latest_main != integration.last_commit_sha:
                integration.last_commit_sha = latest_main
                await integration.save()
        return

    url = f"{server_url}/api/v1/repos/{repo_name}/commits?limit=10"
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return
            commits = await resp.json()
    except Exception as e:
        logging.debug("Poller commits fallback error for %s: %s", repo_name, e)
        return

    if not isinstance(commits, list) or not commits:
        return

    latest_sha = commits[0].get("sha")
    if not latest_sha:
        return

    if integration.last_commit_sha is None:
        integration.last_commit_sha = latest_sha
        await integration.save()
        return

    if latest_sha == integration.last_commit_sha:
        return

    await _send_push_notification(
        session,
        integration,
        server_url,
        token,
        config,
        "main",
        commits,
        integration.last_commit_sha,
    )
    integration.last_commit_sha = latest_sha
    await integration.save()


async def _poll_pull_requests(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
) -> None:
    repo_name = integration.repository_name
    chat = integration.chat
    if not repo_name or not chat:
        return
    url = f"{server_url}/api/v1/repos/{repo_name}/pulls?state=all&sort=recentupdate&limit=20"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Notifier-Poller",
    }

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return
            pulls = await resp.json()
    except Exception as e:
        logging.debug("Poller pulls error for %s: %s", repo_name, e)
        return

    if not isinstance(pulls, list) or not pulls:
        return

    current_pull_map: dict[int, tuple[str, bool]] = {}
    for p in pulls:
        p_id = p.get("id")
        if p_id:
            current_pull_map[p_id] = (
                p.get("state", "open"),
                bool(p.get("merged")),
            )

    is_initial = integration.last_pr_id is None
    max_id = max((p.get("id", 0) for p in pulls), default=0)

    if is_initial:
        _known_pulls[integration.id] = current_pull_map
        integration.last_pr_id = max_id
        await integration.save()
        return

    prev_map = _known_pulls.get(integration.id)
    if prev_map is None:
        prev_map = {}
        for p_id, val in current_pull_map.items():
            if p_id <= (integration.last_pr_id or 0):
                prev_map[p_id] = val

    if await EventSetting.is_enabled(chat.chat_id, "pull_request"):
        for pr in pulls:
            pr_id = pr.get("id")
            if not pr_id:
                continue

            new_state = pr.get("state", "open")
            new_merged = bool(pr.get("merged"))
            prev = prev_map.get(pr_id)

            action: Optional[str] = None
            if prev is None:
                if pr_id > (integration.last_pr_id or 0):
                    action = "opened"
            else:
                old_state, old_merged = prev
                if old_state == "open" and new_state == "closed":
                    action = "closed"
                elif old_state == "closed" and new_state == "open":
                    action = "reopened"
                elif not old_merged and new_merged:
                    action = "closed"

            if action:
                user_obj = pr.get("user") or {}
                username = (
                    user_obj.get("username")
                    or user_obj.get("login")
                    or "user"
                )
                user_html = (
                    user_obj.get("html_url") or f"{server_url}/{username}"
                )

                payload = {
                    "action": action,
                    "number": pr.get("number", 1),
                    "pull_request": {
                        "number": pr.get("number", 1),
                        "title": pr.get("title", ""),
                        "body": pr.get("body", ""),
                        "html_url": (
                            pr.get("html_url")
                            or f"{server_url}/{repo_name}/pulls/{pr.get('number')}"
                        ),
                        "user": {
                            "login": username,
                            "username": username,
                            "html_url": user_html,
                        },
                        "merged": new_merged,
                        "head": {"ref": (pr.get("head") or {}).get("ref", "")},
                        "base": {"ref": (pr.get("base") or {}).get("ref", "")},
                    },
                    "repository": {
                        "full_name": repo_name,
                        "html_url": f"{server_url}/{repo_name}",
                    },
                    "sender": {
                        "login": username,
                        "username": username,
                        "html_url": user_html,
                    },
                }
                ctx = EventCtx(
                    auth_token=token, server_url=server_url, config=config
                )
                msg = build_message("pull_request", payload, ctx)
                if msg:
                    await send_message(session, integration, msg)

    _known_pulls[integration.id] = current_pull_map


async def _poll_issues(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
) -> None:
    repo_name = integration.repository_name
    chat = integration.chat
    if not repo_name or not chat:
        return
    url = f"{server_url}/api/v1/repos/{repo_name}/issues?type=issues&state=all&sort=recentupdate&limit=20"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Notifier-Poller",
    }

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return
            issues = await resp.json()
    except Exception:
        return

    if not isinstance(issues, list) or not issues:
        return

    current_issue_map: dict[int, str] = {}
    for i in issues:
        i_id = i.get("id")
        if i_id and not i.get("pull_request"):
            current_issue_map[i_id] = i.get("state", "open")

    is_initial = integration.last_issue_id is None
    max_id = max(
        (i.get("id", 0) for i in issues if not i.get("pull_request")),
        default=0,
    )

    if is_initial:
        _known_issues[integration.id] = current_issue_map
        integration.last_issue_id = max_id
        await integration.save()
        return

    prev_map = _known_issues.get(integration.id)
    if prev_map is None:
        prev_map = {}
        for i_id, val in current_issue_map.items():
            if i_id <= (integration.last_issue_id or 0):
                prev_map[i_id] = val

    if await EventSetting.is_enabled(chat.chat_id, "issues"):
        for issue in issues:
            if issue.get("pull_request"):
                continue
            i_id = issue.get("id")
            if not i_id:
                continue

            new_state = issue.get("state", "open")
            old_state = prev_map.get(i_id)

            action: Optional[str] = None
            if old_state is None:
                if i_id > (integration.last_issue_id or 0):
                    action = "opened"
            elif old_state == "open" and new_state == "closed":
                action = "closed"
            elif old_state == "closed" and new_state == "open":
                action = "reopened"

            if action:
                user_obj = issue.get("user") or {}
                username = (
                    user_obj.get("username")
                    or user_obj.get("login")
                    or "user"
                )
                user_html = (
                    user_obj.get("html_url") or f"{server_url}/{username}"
                )
                payload = {
                    "action": action,
                    "issue": {
                        "number": issue.get("number", 1),
                        "title": issue.get("title", ""),
                        "html_url": (
                            issue.get("html_url")
                            or f"{server_url}/{repo_name}/issues/{issue.get('number')}"
                        ),
                        "body": issue.get("body", ""),
                        "user": {
                            "login": username,
                            "username": username,
                            "html_url": user_html,
                        },
                    },
                    "repository": {
                        "full_name": repo_name,
                        "html_url": f"{server_url}/{repo_name}",
                    },
                    "sender": {
                        "login": username,
                        "username": username,
                        "html_url": user_html,
                    },
                }
                ctx = EventCtx(
                    auth_token=token, server_url=server_url, config=config
                )
                msg = build_message("issues", payload, ctx)
                if msg:
                    await send_message(session, integration, msg)

    _known_issues[integration.id] = current_issue_map


async def _poll_releases(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
) -> None:
    repo_name = integration.repository_name
    chat = integration.chat
    url = f"{server_url}/api/v1/repos/{repo_name}/releases?limit=5"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Notifier-Poller",
    }

    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return
            releases = await resp.json()
    except Exception:
        return

    if not isinstance(releases, list) or not releases:
        return

    max_id = max((r.get("id", 0) for r in releases), default=0)
    if integration.last_release_id is None:
        integration.last_release_id = max_id
        await integration.save()
        return

    new_releases = [
        r for r in releases if r.get("id", 0) > integration.last_release_id
    ]
    if new_releases and await EventSetting.is_enabled(chat.chat_id, "release"):
        for rel in new_releases:
            author_obj = rel.get("author") or {}
            username = (
                author_obj.get("username")
                or author_obj.get("login")
                or "user"
            )
            author_html = (
                author_obj.get("html_url") or f"{server_url}/{username}"
            )
            payload = {
                "action": "published",
                "release": {
                    "tag_name": rel.get("tag_name", ""),
                    "name": rel.get("name") or rel.get("tag_name", ""),
                    "html_url": (
                        rel.get("html_url")
                        or f"{server_url}/{repo_name}/releases/tag/{rel.get('tag_name')}"
                    ),
                    "body": rel.get("body", ""),
                    "author": {
                        "login": username,
                        "username": username,
                        "html_url": author_html,
                    },
                },
                "repository": {
                    "full_name": repo_name,
                    "html_url": f"{server_url}/{repo_name}",
                },
                "sender": {
                    "login": username,
                    "username": username,
                    "html_url": author_html,
                },
            }
            ctx = EventCtx(auth_token=token, server_url=server_url, config=config)
            msg = build_message("release", payload, ctx)
            if msg:
                await send_message(session, integration, msg)

    if max_id > integration.last_release_id:
        integration.last_release_id = max_id
        await integration.save()


async def poller_loop(config: Config) -> None:
    interval = getattr(config.settings, "poll_interval", 3.0)
    logging.info("Forgejo background poller started (interval: %.1fs)", interval)
    while True:
        try:
            integrations = await Integration.all().prefetch_related("chat", "user")
            if integrations:
                async with aiohttp.ClientSession() as session:
                    for integration in integrations:
                        user = integration.user
                        if not user or not user.token or not integration.repository_name:
                            continue
                        server_url = normalize_server_url(
                            integration.server_url
                            or user.server_url
                            or "https://codeberg.org"
                        )
                        token = user.token

                        await _poll_commits(session, integration, server_url, token, config)
                        await _poll_pull_requests(session, integration, server_url, token, config)
                        await _poll_issues(session, integration, server_url, token, config)
                        await _poll_releases(session, integration, server_url, token, config)
        except Exception as e:
            logging.error("Error in poller_loop: %s", e)

        await asyncio.sleep(interval)
