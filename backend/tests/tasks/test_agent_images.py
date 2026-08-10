from pathlib import Path

import pytest


AGENT_IMAGES = Path(__file__).resolve().parents[2] / "agent-images"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_coding_image_preinstalls_playwright_and_chromium(provider: str) -> None:
    dockerfile = (AGENT_IMAGES / provider / "Dockerfile").read_text()

    assert "ARG PLAYWRIGHT_VERSION=" in dockerfile
    assert 'npm install --global "playwright@${PLAYWRIGHT_VERSION}"' in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in dockerfile
    assert "NODE_PATH=/usr/local/lib/node_modules" in dockerfile
