import pytest


@pytest.fixture(autouse=True)
def disable_background_network(monkeypatch):
    # Individual resolver tests opt in with a mocked ESI client.
    monkeypatch.setenv('NAME_RESOLVER_ENABLED','false')
