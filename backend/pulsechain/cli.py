"""Command line entry points:  python -m pulsechain.cli {reset|train|serve|status}"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import data_gen
from .config import load_settings
from .domain import DEFAULT_AS_OF


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulsechain", description="PulseChain AI utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_reset = sub.add_parser("reset", help="drop and regenerate the synthetic demo database")
    p_reset.add_argument("--as-of", default=DEFAULT_AS_OF.isoformat(), help="Monday of the last observed week (YYYY-MM-DD)")
    p_reset.add_argument("--seed", type=int, default=None)
    p_reset.add_argument("--train", action="store_true", help="also (re)train the forecasting models")
    sub.add_parser("train", help="train the forecasting models from the database")
    p_serve = sub.add_parser("serve", help="run the web app (API + PWA)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    sub.add_parser("status", help="print database and model status")
    args = parser.parse_args(argv)
    settings = load_settings()

    if args.cmd == "reset":
        seed = settings.seed if args.seed is None else args.seed
        summary = data_gen.reset_demo(settings.db_path, as_of=date.fromisoformat(args.as_of), seed=seed)
        print("Demo database reset:", json.dumps(summary))
        if args.train:
            from .ml.train import train_all
            train_all(settings.db_path, settings.model_dir, seed=seed)
        return 0
    if args.cmd == "train":
        from .ml.train import train_all
        report = train_all(settings.db_path, settings.model_dir, seed=settings.seed)
        print(f"Model trained in {report['train_seconds']}s -> {settings.model_dir}")
        return 0
    if args.cmd == "serve":
        from .api.app import create_app, ensure_ready
        app = create_app(settings)
        ensure_ready(settings)
        host, port = args.host or settings.host, args.port or settings.port
        print(f"PulseChain AI running at http://{host}:{port}  (SIMULATED DATA, SMS mode: simulated)")
        app.run(host=host, port=port, threaded=True)
        return 0
    if args.cmd == "status":
        print(json.dumps({"db_exists": settings.db_path.is_file(), "model_exists": (settings.model_dir / "forecast_models.joblib").is_file(),
                          "db_path": str(settings.db_path), "model_dir": str(settings.model_dir)}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
