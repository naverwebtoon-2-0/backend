"""목소리·효과음 프리셋 목록 (§6.4)."""

from fastapi import APIRouter, Depends

from app.deps import require_author
from app.integrations import get_integrations
from app.models import User
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.studio import SfxPreset, Voice

router = APIRouter(tags=["catalog"], responses=ERROR_RESPONSES)


@router.get("/v1/voices", response_model=Page[Voice])
def list_voices(_: User = Depends(require_author)) -> dict:
    return {"items": get_integrations().dubbing.list_voices(), "next_cursor": None}


@router.get("/v1/sfx-presets", response_model=Page[SfxPreset])
def list_sfx_presets(_: User = Depends(require_author)) -> dict:
    return {"items": get_integrations().dubbing.list_sfx_presets(), "next_cursor": None}
