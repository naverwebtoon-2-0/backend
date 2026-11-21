from fastapi import APIRouter

from app.api.v1 import auth, episodes, images, me, series

router = APIRouter()
for module in (auth, me, series, episodes, images):
    router.include_router(module.router)
