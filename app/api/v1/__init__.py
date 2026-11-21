from fastapi import APIRouter

from app.api.v1 import auth, episodes, images, jobs, me, series

router = APIRouter()
for module in (auth, me, series, episodes, images, jobs):
    router.include_router(module.router)
