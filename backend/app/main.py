from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agents import router as agents_router
from app.api.health import router as health_router
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
