"""Claude via the CLI (subscription), with no tools or MCP so the default system prompt isn't paid."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).parent / "prompts"
HAIKU, SONNET = "haiku", "sonnet"


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost: float


class LLMError(Exception):
    pass


def _clean_env() -> dict[str, str]:
    # Don't let the child think it's nested in another Claude Code session.
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN")
            and k not in ("CLAUDECODE", "CLAUDE_PID", "AI_AGENT")}


class Claude:
    def __init__(self, bin_: str = "claude", on_usage=None):
        self.bin = bin_
        self.on_usage = on_usage  # callable(model, alert_id, LLMResult)
        self.sem = asyncio.Semaphore(2)

    async def run(self, model: str, system: str, user: str, alert_id: int = 0, timeout: int = 180) -> LLMResult:
        args = [self.bin, "-p", "--model", model, "--system-prompt", system, "--tools", "",
                "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
                "--setting-sources", "", "--output-format", "json"]
        async with self.sem:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=_clean_env(), cwd="/tmp")
            try:
                out, err = await asyncio.wait_for(proc.communicate(user.encode()), timeout)
            except asyncio.TimeoutError as e:
                proc.kill()
                raise LLMError(f"timeout ({timeout}s)") from e
        try:
            data = json.loads(out)
        except ValueError as e:
            raise LLMError(f"salida no JSON (rc={proc.returncode}): {(out or err)[:300]!r}") from e
        u = data.get("usage") or {}
        res = LLMResult(
            text=str(data.get("result") or ""),
            input_tokens=int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
            + int(u.get("cache_creation_input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cost=float(data.get("total_cost_usd") or 0),
        )
        if self.on_usage:
            self.on_usage(model, alert_id, res)
        if data.get("is_error"):
            raise LLMError(res.text[:300] or "error del CLI")
        return res


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in `text` (tolerates ```json fences and chatter)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise LLMError(f"sin JSON en la respuesta: {text[:200]!r}")
    i = min(starts)
    end = t.rfind("}" if t[i] == "{" else "]")
    try:
        return json.loads(t[i:end + 1])
    except ValueError as e:
        raise LLMError(f"JSON inválido: {text[:200]!r}") from e
