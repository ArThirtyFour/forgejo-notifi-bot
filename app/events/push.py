from typing import Optional

from app.events._base import _Base, GitHubUser, Repository
from app.events._context import EventCtx
from app.events._formatting import _ as _e, truncate
from app.events._registry import register


class CommitAuthor(_Base):
    name: str
    email: Optional[str] = None
    username: Optional[str] = None
    html_url: Optional[str] = None


class Commit(_Base):
    id: str
    message: str
    url: str
    author: CommitAuthor
    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    additions: Optional[int] = None
    deletions: Optional[int] = None


class PushEvent(_Base):
    ref: str
    compare: Optional[str] = None
    compare_url: Optional[str] = None
    commits: list[Commit] = []
    repository: Repository
    sender: Optional[GitHubUser] = None


def commit_message(event: PushEvent, ctx: EventCtx) -> str:
    branch = event.ref.split("/")[-1]
    repo_link_str = (
        f'<a href="{event.repository.html_url}">'
        f"{_e(event.repository.full_name)}:{_e(branch)}</a>"
    )

    if not event.commits:
        return f"<b>📏 On {repo_link_str} new empty push</b>"

    server_url = ctx.server_url or ""
    blocks = []
    for c in event.commits:
        author_name = _e(c.author.name or "unknown")
        username = c.author.username or c.author.name

        if c.author.html_url:
            author_link = f'<a href="{c.author.html_url}">@{_e(username)}</a>'
        elif server_url and username:
            author_link = f'<a href="{server_url}/{_e(username)}">@{_e(username)}</a>'
        elif username:
            author_link = f"@{_e(username)}"
        else:
            author_link = f"<i>{author_name}</i>"

        message_text = truncate(c.message, 500)
        commit_short = c.id[:7] if len(c.id) >= 7 else c.id
        block = (
            f'<blockquote expandable="expandable"><b>Commit '
            f'<a href="{c.url}">#{commit_short}</a> by '
            f"<i>{author_name} ({author_link})</i></b>\n"
            f"<i>{_e(message_text)}</i>\n"
        )

        if c.added:
            block += (
                f"\n<b>🔧 Created files:</b>\n"
                f"<code>{_e(chr(10).join(c.added))}</code>\n"
            )
        if c.removed:
            block += (
                f"\n<b>🗑 Removed files:</b>\n"
                f"<code>{_e(chr(10).join(c.removed))}</code>\n"
            )
        if c.modified:
            block += (
                f"\n<b>🖊 Modified files:</b>\n"
                f"<code>{_e(chr(10).join(c.modified))}</code>\n"
            )

        if c.additions is not None or c.deletions is not None:
            adds = c.additions or 0
            dels = c.deletions or 0
            block += (
                f"\n<b>⌨️ Diff:</b>\n"
                f"➕ {adds}\n➖ {dels}\n"
            )

        block += "</blockquote>"
        blocks.append(block)

    compare_url = event.compare or event.compare_url
    compare_str = f'<a href="{compare_url}">Compare changes</a>\n\n' if compare_url else "\n"

    header = (
        f"<b>📏 On {repo_link_str} new commits!</b>\n"
        f"{len(event.commits)} commits pushed.\n"
        f"{compare_str}"
    )
    return header + "\n".join(blocks)


register("push", PushEvent, commit_message)
