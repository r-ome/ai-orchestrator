import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app.agents.router import router as agents_router
from app.controller.config import get_controller_settings
from app.controller.lifecycle import cancel_task, expiry_loop, reconcile_controller_state
from app.controller.store import get_controller_store
from app.containers.router import router as containers_router
from app.projects.router import router as projects_router
from app.previews.router import router as previews_router
from app.volumes.router import router as volumes_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    controller_settings = get_controller_settings()
    controller_store = get_controller_store()
    await asyncio.to_thread(reconcile_controller_state, controller_store)
    expiry_task = asyncio.create_task(
        expiry_loop(controller_store, controller_settings)
    )
    try:
        yield
    finally:
        await cancel_task(expiry_task)


app = FastAPI(title="Backend API", lifespan=lifespan)
app.include_router(agents_router)
app.include_router(containers_router)
app.include_router(projects_router)
app.include_router(previews_router)
app.include_router(volumes_router)


@app.get("/", response_class=PlainTextResponse)
async def hello_world() -> str:
    return "hello world"
