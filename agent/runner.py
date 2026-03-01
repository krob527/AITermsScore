"""
agent/runner.py – Run the AITermsScore agent for a given AI product/model
and collect the structured scorecard response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import Agent, MessageRole

from config import AppConfig


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScorecardResult:
    product_name: str
    vendor: str
    raw_markdown: str
    structured: Optional[dict] = field(default=None)   # populated by parse_scorecard()
    run_id: Optional[str] = field(default=None)
    thread_id: Optional[str] = field(default=None)


# ──────────────────────────────────────────────────────────────────────────────
# User message builder
# ──────────────────────────────────────────────────────────────────────────────

def _build_user_message(product_name: str) -> str:
    return (
        f"Please evaluate the AI terms of service and legal documents for: **{product_name}**\n\n"
        "Steps to follow:\n"
        "1. Search online for the vendor's current Terms of Service, Privacy Policy, Data Processing "
        "Agreement, Acceptable Use Policy, and any AI-specific usage policies.\n"
        "2. For each rubric criterion, cite the relevant clause(s) you found, assess compliance/risk, "
        "and assign the score defined in the rubric.\n"
        "3. Produce the complete scorecard in the exact Markdown format specified in your instructions.\n"
        "4. Include a JSON block at the end tagged ```json that contains the machine-readable scores.\n"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

def run_scoring(
    client: AgentsClient,
    agent: Agent,
    product_name: str,
    vendor: str = "",
    poll_interval: float = 2.0,
    timeout: float = 300.0,
) -> ScorecardResult:
    """
    Create a new thread, send the scoring request, poll until complete,
    and return a ScorecardResult.
    """
    # Create a fresh thread per product evaluation
    thread = client.threads.create()

    # Post the user message
    client.messages.create(
        thread_id=thread.id,
        role=MessageRole.USER,
        content=_build_user_message(product_name),
    )

    # Create run and poll until terminal state (handles tool calls automatically)
    run = client.runs.create_and_process(
        thread_id=thread.id,
        agent_id=agent.id,
    )

    if run.status != "completed":
        raise RuntimeError(
            f"Agent run ended with status '{run.status}'. "
            f"Last error: {getattr(run, 'last_error', 'unknown')}"
        )

    # Retrieve the assistant's messages for this run
    messages = list(client.messages.list(thread_id=thread.id, run_id=run.id))
    assistant_messages = [
        m for m in messages if m.role == MessageRole.AGENT
    ]
    if not assistant_messages:
        raise RuntimeError("No assistant message found in the completed thread.")

    # Concatenate text content blocks
    raw_md = "\n\n".join(
        block.text.value
        for msg in reversed(assistant_messages)   # chronological order
        for block in msg.content
        if hasattr(block, "text")
    )

    result = ScorecardResult(
        product_name=product_name,
        vendor=vendor or _infer_vendor(product_name),
        raw_markdown=raw_md,
        run_id=run.id,
        thread_id=thread.id,
    )
    result.structured = parse_scorecard(raw_md)
    return result


def _infer_vendor(product_name: str) -> str:
    """Best-effort vendor inference from product name for output filenames."""
    known = {
        "gpt": "openai", "chatgpt": "openai", "openai": "openai",
        "gemini": "google", "bard": "google", "google": "google",
        "claude": "anthropic", "anthropic": "anthropic",
        "copilot": "microsoft", "azure": "microsoft", "microsoft": "microsoft",
        "llama": "meta", "meta": "meta",
        "mistral": "mistral",
        "cohere": "cohere",
    }
    lower = product_name.lower()
    for keyword, vendor in known.items():
        if keyword in lower:
            return vendor
    return "unknown-vendor"


# ──────────────────────────────────────────────────────────────────────────────
# Scorecard parser – extract JSON block from the Markdown response
# ──────────────────────────────────────────────────────────────────────────────

def parse_scorecard(markdown: str) -> dict:
    """
    Extract the machine-readable JSON block (```json ... ```) from the agent
    response and return it as a dict.  Falls back to an empty dict on failure.
    If the agent returned a JSON array (list of clause objects), attempt to
    reshape it into a scores dict; otherwise return {}.
    """
    pattern = re.search(r"```json\s*([\s\S]+?)```", markdown, re.IGNORECASE)
    if not pattern:
        return {}
    try:
        parsed = json.loads(pattern.group(1))
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    # Compute overall from dimension scores if missing or non-numeric
    # Normalize overall to a float (agent sometimes returns it as a string)
    raw_overall = parsed.get("overall")
    if isinstance(raw_overall, str):
        try:
            parsed["overall"] = float(raw_overall)
        except (ValueError, TypeError):
            del parsed["overall"]

    if not isinstance(parsed.get("overall"), (int, float)):
        dim_scores = [
            float(v["score"]) for v in parsed.values()
            if isinstance(v, dict) and (
                isinstance(v.get("score"), (int, float))
                or (isinstance(v.get("score"), str) and v["score"].replace(".", "", 1).isdigit())
            )
        ]
        if dim_scores:
            parsed["overall"] = round(sum(dim_scores) / len(dim_scores), 2)
    return parsed
