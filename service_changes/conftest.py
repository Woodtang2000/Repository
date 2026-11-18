import pytest


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Notices pace their sends (announce.EMAIL_GAP) so Bird doesn't refuse a burst; tests don't need to wait."""
    from . import announce
    monkeypatch.setattr(announce, "EMAIL_GAP", 0)
    monkeypatch.setattr(announce, "TEXT_GAP", 0)
