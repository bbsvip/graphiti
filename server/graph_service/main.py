from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from graph_service.config import Settings, get_settings
from graph_service.mcp_runtime import create_mcp_app, mcp_lifespan
from graph_service.routers import admin, ingest, retrieve
from graph_service.zep_graphiti import configured_graphiti


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    async with configured_graphiti(settings, live_config=True) as client:
        await client.build_indices_and_constraints()
        async with mcp_lifespan(settings, client):
            yield


async def healthcheck() -> JSONResponse:
    return JSONResponse(content={'status': 'healthy'}, status_code=200)


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(retrieve.router)
    app.include_router(ingest.router)
    app.include_router(admin.router)
    app.include_router(admin.callback_router)
    app.add_api_route('/healthcheck', healthcheck, methods=['GET'])
    app.mount('/mcp', create_mcp_app(app.state.settings))
    return app


app = create_app()
