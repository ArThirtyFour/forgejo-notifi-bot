"""Shared Pydantic models — types that appear in multiple event payloads."""
from typing import Optional

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class GitHubUser(_Base):
    login: Optional[str] = None
    username: Optional[str] = None
    html_url: Optional[str] = None
    id: Optional[int] = None

    @property
    def handle(self) -> str:
        return self.username or self.login or "user"


class Repository(_Base):
    full_name: str
    name: Optional[str] = None
    html_url: str
    private: bool = False
    stars_count: int = 0
    stargazers_count: int = 0
    forks: int = 0
    forks_count: int = 0


class BranchRef(_Base):
    ref: Optional[str] = None
    sha: Optional[str] = None


class Issue(_Base):
    number: int
    title: str
    html_url: str
    user: GitHubUser
    body: Optional[str] = None
    pull_request: Optional[dict] = None


class PullRequest(_Base):
    number: int
    title: str
    html_url: str
    user: GitHubUser
    body: Optional[str] = None
    merged: Optional[bool] = None
    base: Optional[BranchRef] = None
    head: Optional[BranchRef] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None
    changed_files: Optional[int] = None


class Comment(_Base):
    body: Optional[str] = None
    html_url: str
    user: GitHubUser
    path: Optional[str] = None


class Discussion(_Base):
    number: int
    title: str
    html_url: str
    body: Optional[str] = None
    user: GitHubUser
