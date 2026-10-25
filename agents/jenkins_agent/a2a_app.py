"""A2A Application for Jenkins Investigation Agent (Port 9101)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import uvicorn

from agents.jenkins_agent.agent import run_investigation
from common.a2a_server import create_a2a_app

# Load card data
card_path = Path(__file__).parents[2] / "a2a" / "jenkins_agent.json"
if card_path.exists():
    with open(card_path, "r", encoding="utf-8") as f:
        card_data = json.load(f)
else:
    card_data = {
        "name": "jenkins_agent",
        "description": "Analyzes Jenkins CI build logs and failures.",
        "version": "1.0.0",
        "url": os.getenv("JENKINS_AGENT_URL", "http://localhost:9101"),
        "skills": [{"name": "investigate_incident"}],
    }

card_data["url"] = os.getenv("JENKINS_AGENT_URL", "http://localhost:9101")

app = create_a2a_app(
    agent_card_data=card_data,
    run_fn=run_investigation,
    timeout_seconds=float(os.getenv("A2A_TIMEOUT_SECONDS", "60.0")),
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "9101"))
    uvicorn.run(app, host="0.0.0.0", port=port)
