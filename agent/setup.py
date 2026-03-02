"""
agent/setup.py – Create or retrieve the AITermsScore agent inside the
DevGenius AI Foundry project.

The agent is created once and reused across runs (idempotent).
DuckDuckGo search is attached so the agent can search live vendor legal documents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import (
    Agent,
    FunctionTool,
    ToolSet,
)
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import DefaultAzureCredential
from ddgs import DDGS

# Per-request HTTP timeouts for all Azure AI Foundry API calls.
# Prevents individual SDK calls from blocking indefinitely on slow/stuck connections.
_AZURE_TRANSPORT = RequestsTransport(connection_timeout=30, read_timeout=60)

from config import AppConfig


def web_search(query: str) -> str:
    """
    Search the web using DuckDuckGo and return a summary of the top results.

    :param query: The search query string to look up on the web.
    :return: A formatted string containing titles, URLs, and snippets from the top results.
    """
    results = list(DDGS(timeout=10).text(query, max_results=8))
    if not results:
        return "No results found."
    lines = []
    for r in results:
        lines.append(f"Title: {r.get('title', '')}")
        lines.append(f"URL:   {r.get('href', '')}")
        lines.append(f"Body:  {r.get('body', '')}")
        lines.append("")
    return "\n".join(lines)


def _read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {path}\n"
            "Please populate the placeholder file before running."
        )
    return path.read_text(encoding="utf-8").strip()


def _load_rubric(path: Path) -> str:
    """Load a rubric file and return it as formatted text.

    Supports JSON rubric files (*.json) and plain-text/Markdown files.
    JSON rubrics are rendered into a structured, human-readable format
    suitable for embedding in a system prompt.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Rubric file not found: {path}\n"
            "Please ensure the rubric file is present before running."
        )

    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return _format_rubric_json(data)

    return path.read_text(encoding="utf-8").strip()


def _format_rubric_json(data: dict) -> str:
    """Convert a structured JSON rubric into a human-readable text block."""
    lines: list[str] = []

    meta = data.get("rubric_metadata", {})
    lines.append(f"# {meta.get('name', 'Rubric')}  (v{meta.get('version', '')})")    
    scope = meta.get("scope")
    if scope:
        lines.append(f"Scope: {scope}")
    sources = meta.get("evidence_sources", [])
    if sources:
        lines.append("Evidence sources: " + ", ".join(sources))
    lines.append("")

    # Scoring scale
    scale = data.get("scoring_scale", [])
    if scale:
        lines.append("## Scoring Scale")
        for entry in scale:
            lines.append(
                f"  {entry['score']} – {entry['label']}: {entry['definition']}"
            )
        lines.append("")

    # Dimensions
    dimensions = data.get("dimensions", [])
    weights = data.get("weights", {})
    if dimensions:
        lines.append("## Dimensions")
        for dim in dimensions:
            weight = weights.get(dim["name"])
            weight_str = f" (weight: {int(weight * 100)}%" + ")" if weight is not None else ""
            lines.append(f"### {dim['id']} – {dim['name']}{weight_str}")
            lines.append(f"Description: {dim['description']}")
            lines.append(f"Key question: {dim['key_question']}")
            lines.append("Indicators:")
            for ind in dim.get("indicators", []):
                lines.append(f"  - {ind['id']}: {ind['description']}")
            lines.append("")

    # Final grades
    grades = data.get("final_grades", [])
    if grades:
        lines.append("## Final Grade Thresholds")
        for g in grades:
            lines.append(
                f"  {g['grade']}: {g['min_score']}–{g['max_score']} → {g['interpretation']}"
            )

    return "\n".join(lines)


def get_or_create_agent(cfg: AppConfig) -> tuple[AgentsClient, Agent]:
    """
    Connect to the AI Foundry project and return (agents_client, agent).

    If the AGENT_ID environment variable is set the agent is fetched directly
    by ID (no list call required – avoids the agents/read list permission on
    Azure App Service).  Otherwise the agents list is searched by name and a
    new agent is created if none is found.
    """
    credential = DefaultAzureCredential()
    agents_client = AgentsClient(
        endpoint=cfg.project_endpoint,
        credential=credential,
        transport=_AZURE_TRANSPORT,
    )

    # Read system prompt and rubric from local files
    system_prompt = _read_text(cfg.system_prompt_path)
    rubric_text = _load_rubric(cfg.rubric_path)

    # Embed the rubric directly into the system prompt
    full_instructions = f"{system_prompt}\n\n---\n## RUBRIC\n\n{rubric_text}"

    # Build DuckDuckGo search toolset for agent registration
    # NOTE: do NOT call enable_auto_function_calls() here – runner.py handles
    # tool execution manually via a polling loop + submit_tool_outputs().
    # Calling enable_auto_function_calls() would intercept REQUIRES_ACTION
    # responses at the SDK layer, preventing the runner's loop from ever firing.
    toolset = ToolSet()
    toolset.add(FunctionTool({web_search}))

    # --- Fast path: AGENT_ID env var is set (App Service / CI) ---------------
    # Construct a lightweight stub so zero API calls are made during setup.
    # The runner only ever uses agent.id, so this is safe.
    agent_id_env = os.getenv("AGENT_ID", "").strip()
    if agent_id_env:
        agent = SimpleNamespace(id=agent_id_env)
        return agents_client, agent  # type: ignore[return-value]

    # --- Slow path: search by name then create if missing (local dev) ---------
    existing = None
    for a in agents_client.list_agents():
        if a.name == cfg.agent_name:
            existing = a
            break

    if existing is not None:
        agent = agents_client.update_agent(
            agent_id=existing.id,
            model=cfg.model_deployment,
            instructions=full_instructions,
            toolset=toolset,
        )
        return agents_client, agent

    # Create a brand-new agent
    agent = agents_client.create_agent(
        model=cfg.model_deployment,
        name=cfg.agent_name,
        instructions=full_instructions,
        toolset=toolset,
    )
    return agents_client, agent


def delete_agent(cfg: AppConfig) -> None:
    """Delete the registered agent from AI Foundry (cleanup helper)."""
    credential = DefaultAzureCredential()
    agents_client = AgentsClient(
        endpoint=cfg.project_endpoint,
        credential=credential,
        transport=_AZURE_TRANSPORT,
    )
    for a in agents_client.list_agents():
        if a.name == cfg.agent_name:
            agents_client.delete_agent(a.id)
            print(f"Deleted agent: {a.id}")
            return
    print(f"No agent named '{cfg.agent_name}' found.")
