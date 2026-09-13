import asyncio
import logging
from typing import Any

import aiohttp
from aiogram import Bot

from app.config import Config
from app.db.functions import EventSetting, Integration, User
from app.events import EventCtx, build_message
from app.utils.forgejo import normalize_server_url
from app.webhook.api import send_message

POLL_INTERVAL = 15.0


async def _poll_commits(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
) -> None:
    repo_name = integration.repository_name
    chat = integration.chat
    url = f"{server_url}/api/v1/repos/{repo_name}/commits?limit=10"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Notifier-Poller",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return
            commits = await resp.json()
    except Exception as e:
        logging.debug("Poller commits error for %s: %s", repo_name, e)
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

    new_commits = []
    for c in commits:
        if c.get("sha") == integration.last_commit_sha:
            break
        new_commits.append(c)

    if not new_commits:
        integration.last_commit_sha = latest_sha
        await integration.save()
        return

    logging.info(
        "Poller detected %d new commit(s) for %s", len(new_commits), repo_name
    )

    if await EventSetting.is_enabled(chat.chat_id, "push"):
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
            username = author_obj.get("username") or author_obj.get("login") or author_name
            author_html_url = author_obj.get("html_url") or f"{server_url}/{username}"

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
        sender_html_url = top_author.get("html_url") or f"{server_url}/{sender_login}"

        payload = {
            "ref": "refs/heads/main",
            "compare": f"{server_url}/{repo_name}/compare/{integration.last_commit_sha}...{latest_sha}",
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

    integration.last_commit_sha = latest_sha
    await integration.save()


async def _poll_issues(
    session: aiohttp.ClientSession,
    integration: Integration,
    server_url: str,
    token: str,
    config: Config,
) -> None:
    repo_name = integration.repository_name
    chat = integration.chat
    url = f"{server_url}/api/v1/repos/{repo_name}/issues?state=all&limit=5"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "Forgejo-Notifier-Poller",
    }

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return
            issues = await resp.json()
    except Exception:
        return

    if not isinstance(issues, list) or not issues:
        return

    max_id = max((i.get("id", 0) for i in issues), default=0)
    if integration.last_issue_id is None:
        integration.last_issue_id = max_id
        await integration.save()
        return

    new_issues = [i for i in issues if i.get("id", 0) > integration.last_issue_id]
    if new_issues and await EventSetting.is_enabled(chat.chat_id, "issues"):
        for issue in new_issues:
            user_obj = issue.get("user") or {}
            username = user_obj.get("username") or user_obj.get("login") or "user"
            user_html = user_obj.get("html_url") or f"{server_url}/{username}"
            payload = {
                "action": "opened",
                "issue": {
                    "number": issue.get("number", 1),
                    "title": issue.get("title", ""),
                    "html_url": issue.get("html_url") or f"{server_url}/{repo_name}/issues/{issue.get('number')}",
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
            ctx = EventCtx(auth_token=token, server_url=server_url, config=config)
            msg = build_message("issues", payload, ctx)
            if msg:
                await send_message(session, integration, msg)

    if max_id > integration.last_issue_id:
        integration.last_issue_id = max_id
        await integration.save()


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
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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

    new_releases = [r for r in releases if r.get("id", 0) > integration.last_release_id]
    if new_releases and await EventSetting.is_enabled(chat.chat_id, "release"):
        for rel in new_releases:
            author_obj = rel.get("author") or {}
            username = author_obj.get("username") or author_obj.get("login") or "user"
            author_html = author_obj.get("html_url") or f"{server_url}/{username}"
            payload = {
                "action": "published",
                "release": {
                    "tag_name": rel.get("tag_name", ""),
                    "name": rel.get("name") or rel.get("tag_name", ""),
                    "html_url": rel.get("html_url") or f"{server_url}/{repo_name}/releases/tag/{rel.get('tag_name')}",
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
    logging.info("Forgejo background poller started (interval: %.0fs)", POLL_INTERVAL)
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
                            integration.server_url or user.server_url or "https://codeberg.org"
                        )
                        token = user.token

                        await _poll_commits(session, integration, server_url, token, config)
                        await _poll_issues(session, integration, server_url, token, config)
                        await _poll_releases(session, integration, server_url, token, config)
        except Exception as e:
            logging.error("Error in poller_loop: %s", e)

        await asyncio.sleep(POLL_INTERVAL)
