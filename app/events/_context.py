"""Per-event context passed to formatters."""
from dataclasses import dataclass
from typing import Optional

from app.config import Config


@dataclass
class EventCtx:
    """Context a formatter receives alongside the event payload."""
    auth_token: Optional[str] = None
    server_url: Optional[str] = None
    config: Optional[Config] = None
