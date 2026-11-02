from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_shape():
    text = (ROOT / "Dockerfile").read_text()
    assert "USER 1000" in text and 'ENTRYPOINT ["history"]' in text
    assert "node" not in text.lower()
    assert "claude" not in text.lower()
    assert "uv sync --frozen --no-dev" in text
