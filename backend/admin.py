# admin.py
from fastapi import APIRouter, Depends
from backend.auth import verify_api_key
from backend.cache import clear_cache

router = APIRouter()

@router.post("/admin/clear-cache", dependencies=[Depends(verify_api_key)])
async def clear_cache_endpoint():
    await clear_cache()
    return {"status": "ok", "message": "Cache cleared"}

