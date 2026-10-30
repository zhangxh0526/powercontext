"""Runtime configuration for the experiment continuation assistant."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SCOPE_ID_MAX = 256
TEXT_MAX_BYTES = 8192
DEFAULT_RECALL_QUERY = "上次进行到哪里，下一步是什么？"
# The FTS-only service tokenizes whole runs of CJK text, so a Chinese question
# rarely shares a token with the stored sentence. Both are kept as anchors.
RECALL_ANCHOR = "实验进展"


@dataclass(frozen=True)
class Config:
    powercontext_url: str
    powercontext_token: str | None
    powercontext_scope: str | None
    data_dir: str
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return self.powercontext_url.rstrip("/")

    @property
    def scope_is_configured(self) -> bool:
        return bool(self.powercontext_scope)


def _clean_url(value: str) -> str:
    if not re.match(r"^https?://", value):
        raise ValueError("POWERCONTEXT_URL must start with http:// or https://")
    return value.rstrip("/")


def load_config(environ: dict[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    url = _clean_url(env.get("POWERCONTEXT_URL", "http://127.0.0.1:8000"))
    token = (env.get("POWERCONTEXT_TOKEN") or "").strip() or None
    scope = (env.get("POWERCONTEXT_SCOPE") or "").strip() or None
    if scope and (len(scope) > SCOPE_ID_MAX or not scope.strip()):
        raise ValueError("POWERCONTEXT_SCOPE must be a non-empty Scope id of at most 256 characters")
    data_dir = env.get("ASSISTANT_DATA_DIR", os.path.join(os.getcwd(), "data"))
    host = env.get("ASSISTANT_HOST", "127.0.0.1")
    port = int(env.get("ASSISTANT_PORT", "8710"))
    return Config(
        powercontext_url=url,
        powercontext_token=token,
        powercontext_scope=scope,
        data_dir=os.path.abspath(data_dir),
        host=host,
        port=port,
    )
