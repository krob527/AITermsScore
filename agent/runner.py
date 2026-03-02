"""
agent/runner.py – Run the AITermsScore agent for a given AI product/model
and collect the structured scorecard response.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import Agent, MessageRole, RunStatus, ToolOutput

from config import AppConfig

# Hard per-call timeout for individual Azure AI Foundry SDK calls (seconds).
# Prevents any single blocking HTTP call from hanging indefinitely regardless
# of transport-level timeout configuration.
_SDK_CALL_TIMEOUT = 45

# Module-level thread pool.  IMPORTANT: do NOT use `with ThreadPoolExecutor`
# for _sdk_call — `with` calls shutdown(wait=True) on exit, which would block
# indefinitely waiting for a stuck HTTP thread to finish, defeating the whole
# purpose.  Using a persistent pool means timed-out threads are simply
# abandoned (they'll eventually be cleaned up by the OS when the process exits
# or the underlying socket times out).
_SDK_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="sdk")
_TRACE_SDK_CALLS = os.getenv("TRACE_SDK_CALLS", "0").strip().lower() in {"1", "true", "yes", "on"}


def _sdk_call(fn: Callable, **kwargs):
    """Execute an SDK call with a hard _SDK_CALL_TIMEOUT deadline.

    Submits the call to _SDK_POOL and waits at most _SDK_CALL_TIMEOUT seconds.
    If the future doesn't complete in time, RuntimeError is raised immediately —
    the stuck worker thread is left to time out on its own without blocking the caller.
    """
    future = _SDK_POOL.submit(fn, **kwargs)
    try:
        return future.result(timeout=_SDK_CALL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        raise RuntimeError(
            f"Azure AI Foundry SDK call timed out after {_SDK_CALL_TIMEOUT}s "
            f"({getattr(fn, '__qualname__', str(fn))}). "
            "The service may be slow or unreachable."
        )


def _sdk_call_timed(label: str, fn: Callable, on_status: Optional[Callable[[str], None]] = None, **kwargs):
    """Execute an SDK call and optionally emit timing diagnostics."""
    started = time.perf_counter()
    result = _sdk_call(fn, **kwargs)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if _TRACE_SDK_CALLS and on_status is not None:
        on_status(f"[trace] {label}: {elapsed_ms} ms")
    return result


def _status_text(status: object) -> str:
    """Normalize Azure run status to lowercase text like 'requires_action'."""
    if isinstance(status, RunStatus):
        return str(status.value).lower()
    text = str(status).lower()
    if text.startswith("runstatus."):
        return text.split(".", 1)[1]
    return text


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
    on_status: Optional[Callable[[str], None]] = None,
) -> ScorecardResult:
    """
    Create a new thread, send the scoring request, manually poll until complete
    (intercepting tool calls to emit live status messages), and return a ScorecardResult.
    """
    from agent.setup import web_search  # local import avoids circular dependency at module load

    _tool_fn_map: dict[str, Callable] = {"web_search": web_search}
    run_started_at = time.monotonic()
    last_progress_ping_at = run_started_at

    # Create a fresh thread per product evaluation
    thread = _sdk_call_timed("threads.create", client.threads.create, on_status=on_status)

    # Post the user message
    _sdk_call_timed(
        "messages.create",
        client.messages.create,
        on_status=on_status,
        thread_id=thread.id,
        role=MessageRole.USER,
        content=_build_user_message(product_name),
    )

    # Start the run (does NOT auto-execute tools)
    run = _sdk_call_timed(
        "runs.create",
        client.runs.create,
        on_status=on_status,
        thread_id=thread.id,
        agent_id=agent.id,
    )

    _TERMINAL = {"completed", "failed", "cancelled", "expired"}
    deadline = time.monotonic() + timeout  # wall-clock deadline

    while _status_text(run.status) not in _TERMINAL:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Agent run timed out after {timeout}s (status: {run.status}). "
                "The Azure AI Foundry agent may be slow or unresponsive."
            )
        time.sleep(poll_interval)
        run = _sdk_call_timed(
            "runs.get",
            client.runs.get,
            on_status=on_status,
            thread_id=thread.id,
            run_id=run.id,
        )

        if on_status and time.monotonic() - last_progress_ping_at >= 20:
            elapsed_sec = int(time.monotonic() - run_started_at)
            on_status(f"Still running ({_status_text(run.status)}), {elapsed_sec}s elapsed…")
            last_progress_ping_at = time.monotonic()

        if _status_text(run.status) == "requires_action":
            tool_outputs: list[ToolOutput] = []
            tool_calls = run.required_action.submit_tool_outputs.tool_calls
            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                # Emit visible status so it appears in the progress log
                if on_status:
                    query = fn_args.get("query", fn_name)
                    on_status(f"Searching: {str(query)[:90]}…")

                # Execute the tool locally
                fn = _tool_fn_map.get(fn_name)
                if fn is None:
                    output = f"Error: unknown tool '{fn_name}'"
                else:
                    try:
                        output = fn(**fn_args)
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        output = f"Tool error: {exc}"

                tool_outputs.append(ToolOutput(tool_call_id=tc.id, output=str(output)))

            # Return results to the agent and continue the run
            run = _sdk_call_timed(
                "runs.submit_tool_outputs",
                client.runs.submit_tool_outputs,
                on_status=on_status,
                thread_id=thread.id,
                run_id=run.id,
                tool_outputs=tool_outputs,
            )

    if _status_text(run.status) != "completed":
        raise RuntimeError(
            f"Agent run ended with status '{run.status}'. "
            f"Last error: {getattr(run, 'last_error', 'unknown')}"
        )

    # Retrieve the assistant's messages for this run
    messages = list(
        _sdk_call_timed(
            "messages.list",
            client.messages.list,
            on_status=on_status,
            thread_id=thread.id,
            run_id=run.id,
        )
    )
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
