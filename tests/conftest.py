from pathlib import Path

import pytest

from audio_assistant.client.team_loader import load_team

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def team():
    return load_team(ROOT / "config" / "team.yaml")
