"""Write a one-line outcome to the GitHub Actions run summary.

Production state should be readable from the Actions page without opening a log
or asking anyone. A green checkmark says the process exited zero; it does not
say whether 21 cells were promoted or zero, whether the recorder wrote deltas
or sat idle, or which commit ran.

Writes to $GITHUB_STEP_SUMMARY when present and is a silent no-op otherwise, so
local runs are unaffected. Never raises: a reporting failure must not fail the
job it is reporting on.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def write_summary(headline: str, detail: str = "", ok: bool = True) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    sha = os.environ.get("GITHUB_SHA", "")[:8]
    mark = "✅" if ok else "❌"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"### {mark} {headline}\n\n")
            if detail:
                handle.write(f"```\n{detail}\n```\n\n")
            if sha:
                handle.write(f"`commit {sha}`\n")
    except Exception:
        logger.warning("Could not write run summary", exc_info=True)
