"""
Main entrypoint — runs the FastAPI server (which embeds the orchestrator).

    python scripts/run.py                        # API + scheduler on :8000
    python scripts/run.py --port=9000
    python scripts/run.py --sources=config/sources.yaml
    python scripts/run.py --worker-only          # scheduler only, no HTTP
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _configure_logging() -> None:
    import yaml
    log_config = Path("config/logging.yaml")
    if log_config.exists():
        import logging.config
        with log_config.open() as f:
            logging.config.dictConfig(yaml.safe_load(f))
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


def main() -> None:
    _configure_logging()

    sources_path = "config/sources.yaml"
    port = 8000
    worker_only = False

    for arg in sys.argv[1:]:
        if arg.startswith("--sources="):
            sources_path = arg.split("=", 1)[1]
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg == "--worker-only":
            worker_only = True

    if worker_only:
        import asyncio
        from src.agents.orchestrator import Orchestrator
        orch = Orchestrator(sources_path=sources_path)
        asyncio.run(orch.start())
    else:
        import uvicorn
        from src.api.app import create_app
        app = create_app(sources_path=sources_path)
        uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
