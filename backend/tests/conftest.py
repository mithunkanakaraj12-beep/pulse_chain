"""Shared fixtures. The ML model is trained once per test session (~20 s) on a temporary synthetic database."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulsechain import data_gen  # noqa: E402
from pulsechain.api.app import create_app  # noqa: E402
from pulsechain.config import Settings  # noqa: E402
from pulsechain.ml.train import train_all  # noqa: E402


def make_settings(tmp: Path, **overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=0, db_path=tmp / "test.db", model_dir=tmp / "models", seed=42,
        alert_risk_threshold="high", sms_mode="simulated", sms_allow_live=False, twilio_account_sid="",
        twilio_auth_token="", twilio_from_number="", open_meteo_enabled=False, open_meteo_timeout=1.0,
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="session")
def trained_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pulsechain")
    settings = make_settings(tmp)
    data_gen.reset_demo(settings.db_path, seed=settings.seed)
    train_all(settings.db_path, settings.model_dir, seed=settings.seed, verbose=False)
    return settings


@pytest.fixture()
def settings(trained_env):
    """Fresh database for every test (model is shared)."""
    data_gen.reset_demo(trained_env.db_path, seed=trained_env.seed)
    return trained_env


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    return app.test_client()


@pytest.fixture()
def platform(client):
    return client.application.extensions["pulsechain"]
