import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.agents import router as agents_router
from app.api.health import router as health_router
from app.api.run import router as run_router
from app.api.settings import router as settings_router
from app.api.tools import router as tools_router
from app.config import settings

app = FastAPI(title="Corp Agent Client API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(settings_router)
app.include_router(tools_router)
app.include_router(agents_router)
app.include_router(run_router)

# In production (LOCAL_DEV=false), serve the React build as static files.
# The deploy script copies frontend/dist → backend/static before uploading.
if not settings.local_dev:
    _static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.isdir(_static_dir):
        app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
