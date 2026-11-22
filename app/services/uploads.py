"""업로드 검증: content_type 화이트리스트, 크기 상한, 실제 객체(HEAD + 매직바이트) 확인."""

from app.config import get_settings
from app.errors import ApiError, validation_error
from app.integrations import get_integrations
from app.services.media import ALLOWED_IMAGE_TYPES, HEADER_BYTES, sniff_image


def unsupported(content_type: str, **details: object) -> ApiError:
    return ApiError(
        415,
        "UNSUPPORTED_MEDIA",
        "지원하지 않는 이미지 형식입니다. (PNG, JPEG, WebP)",
        {"content_type": content_type, **details},
    )


def too_large(size: int, **details: object) -> ApiError:
    limit = get_settings().max_image_bytes
    return ApiError(
        413, "FILE_TOO_LARGE", "이미지 크기가 제한을 넘었습니다.", {"size": size, "limit": limit, **details}
    )


def validate_file_spec(content_type: str, size: int, **details: object) -> None:
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise unsupported(content_type, **details)
    if size > get_settings().max_image_bytes:
        raise too_large(size, **details)


def verify_uploaded_image(
    key: str, declared_type: str, declared_size: int, **details: object
) -> tuple[int | None, int | None]:
    """실제 업로드된 객체를 검사하고 (width, height)를 돌려준다."""
    storage = get_integrations().storage
    info = storage.head(key)
    if info is None:
        raise validation_error("업로드된 파일을 찾을 수 없습니다.", reason="object_not_found", **details)
    if info.size > get_settings().max_image_bytes:
        raise too_large(info.size, **details)
    if info.size != declared_size:
        raise validation_error(
            "업로드된 파일 크기가 신청한 크기와 다릅니다.",
            reason="size_mismatch",
            declared=declared_size,
            actual=info.size,
            **details,
        )
    sniffed = sniff_image(storage.get_range(key, min(HEADER_BYTES, info.size)))
    if sniffed is None or sniffed[0] != declared_type:
        raise unsupported(declared_type, reason="content_mismatch", **details)
    return sniffed[1], sniffed[2]
