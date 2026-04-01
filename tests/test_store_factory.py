from __future__ import annotations

from config import Config
from models.db import InMemoryStore, create_store


def test_store_factory_falls_back_to_memory_for_missing_sql_driver(monkeypatch):
    monkeypatch.setattr(Config, "STORE_BACKEND", "sql", raising=False)
    monkeypatch.setattr(Config, "DATABASE_URL", "mysql+pymysql://invalid", raising=False)

    store = create_store(Config)

    assert isinstance(store, InMemoryStore)
