from fastapi import APIRouter

from app.api.v1 import (
    auth,
    catalog,
    characters,
    episodes,
    images,
    jobs,
    me,
    public,
    publish,
    series,
    studio,
)

router = APIRouter()
for module in (auth, me, series, episodes, images, jobs, studio, characters, catalog, publish, public):
    router.include_router(module.router)
