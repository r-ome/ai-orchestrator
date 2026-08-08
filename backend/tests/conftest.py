import pytest

from app.controller.config import get_controller_settings


@pytest.fixture(autouse=True)
def _isolated_controller_database(tmp_path, monkeypatch):
    """Points every test's controller store at a throwaway tmp_path.

    Without this, any test that exercises a router endpoint resolves the
    get_controller_store dependency and writes to the live database at
    backend/.controller-data/controller.sqlite3. get_controller_settings is
    lru_cache'd, so setting the env var alone is not enough: the cache must
    be cleared after the env var changes, and again on teardown, or a stale
    ControllerSettings leaks into the next test. The environment variable is
    set unconditionally: honouring an ambient CONTROLLER_DATA_DIRECTORY would
    silently point the suite at whatever real directory a shell exported. A
    test that needs its own location still wins, because its own setenv runs
    after this fixture.
    """
    monkeypatch.setenv("CONTROLLER_DATA_DIRECTORY", str(tmp_path / "controller-data"))
    get_controller_settings.cache_clear()
    yield
    get_controller_settings.cache_clear()
