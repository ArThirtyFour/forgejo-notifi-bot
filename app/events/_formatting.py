"""Small helpers shared between formatters (HTML escaping, link helpers, truncation)."""
from html import escape as _escape
from typing import Optional

from app.events._base import GitHubUser, Repository

_ = _escape


def truncate(text: Optional[str], limit: int = 400) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def user_link(user: GitHubUser) -> str:
    handle = user.handle
    if user.html_url:
        return f'<a href="{user.html_url}">@{_(handle)}</a>'
    return f'@{_(handle)}'


def repo_link(repo: Repository) -> str:
    return f'<a href="{repo.html_url}">{_(repo.full_name)}</a>'
