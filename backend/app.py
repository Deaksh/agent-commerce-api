# app.py
import logging
from fastapi import FastAPI
from cache import init_cache, close_cache
from audit import router as audit_router
from admin import router as admin_router



logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-commerce")

app = FastAPI(title="Agent-Optimized Commerce API", version="0.4.0")

# Routers
app.include_router(audit_router)
app.include_router(admin_router)

@app.on_event("startup")
async def startup_event():
    await init_cache()

@app.on_event("shutdown")
async def shutdown_event():
    await close_cache()
