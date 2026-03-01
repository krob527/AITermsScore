"""
config.py – Load and validate environment configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root; override=True ensures .env takes precedence over
# any stale shell-level env vars that may still contain placeholder values.
load_dotenv(Path(__file__).parent / ".env", override=True)


@dataclass(frozen=True)
class AppConfig:
    project_endpoint: str
    model_deployment: str
    agent_name: str

    # Derived paths
    rubric_path: Path
    system_prompt_path: Path
    output_dir: Path


def load_config() -> AppConfig:
    """Read configuration from environment variables and validate required fields."""

    def _require(key: str) -> str:
        value = os.getenv(key, "").strip()
        if not value or "<" in value:
            raise EnvironmentError(
                f"Required environment variable '{key}' is not set or still contains a placeholder. "
                f"Copy .env.example → .env and fill in your values."
            )
        return value

    root = Path(__file__).parent

    # On Azure App Service WEBSITE_SITE_NAME is always set; /home persists across restarts.
    # Locally fall back to ./output next to this file.
    if os.getenv("WEBSITE_SITE_NAME"):
        output_dir = Path("/home/output")
    else:
        output_dir = Path(os.getenv("OUTPUT_DIR", str(root / "output")))

    cfg = AppConfig(
        project_endpoint=_require("AZURE_AI_PROJECT_ENDPOINT"),
        model_deployment=_require("AZURE_AI_MODEL_DEPLOYMENT"),
        agent_name=os.getenv("AGENT_NAME", "AITermsScoreAgent"),
        rubric_path=root / "rubric" / "ai_terms_risk_rubric.v1.0.0.json",
        system_prompt_path=root / "prompt" / "system_prompt.md",
        output_dir=output_dir,
    )

    # Ensure output directory exists
    cfg.output_dir.mkdir(exist_ok=True)

    return cfg
