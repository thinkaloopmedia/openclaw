from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.agents.orchestrator import Orchestrator
from src.api.routes import router
from src.scoring.engine import load_scoring_config


def create_app(sources_path: str = "config/sources.yaml") -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        orch = Orchestrator(sources_path=sources_path)
        await orch.setup()
        app.state.orchestrator = orch
        app.state.scoring_config = load_scoring_config(sources_path)
        yield
        await orch.stop()

    app = FastAPI(
        title="openclaw",
        description="Autonomous product data retrieval API",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
