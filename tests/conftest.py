from __future__ import annotations

import os

import pytest

from app import create_app


@pytest.fixture()
def client():
    os.environ["STORE_BACKEND"] = "memory"
    os.environ["USE_STUB_AI"] = "true"
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
