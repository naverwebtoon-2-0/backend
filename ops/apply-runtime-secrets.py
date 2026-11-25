#!/usr/bin/env python3
"""GitHub Actions에서 전달된 운영 연동 설정을 반영한다."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

KEYS = (
    "AUTHOR_INVITE_CODES",
    "STORAGE_BUCKET",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "CLOVA_OCR_INVOKE_URL",
    "CLOVA_OCR_SECRET",
    "CLOVA_STUDIO_API_KEY",
    "CLOVA_VOICE_CLIENT_ID",
    "CLOVA_VOICE_CLIENT_SECRET",
)
path = pathlib.Path("/etc/naverwebtoon-backend/backend.env")
values = json.load(sys.stdin)
if set(values) != set(KEYS) or any(
    not isinstance(v, str) or not v or any(c in v for c in "\r\n'\0") for v in values.values()
):
    sys.exit("연동 설정 형식이 올바르지 않습니다.")
values.update(AI_BACKEND="clova", STORAGE_BACKEND="s3")
old = path.read_text()
lines = [line for line in old.splitlines() if line.partition("=")[0] not in values]
new = "\n".join(lines + [f"{key}='{value}'" for key, value in values.items()]) + "\n"


def write(content):
    fd, tmp = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def compose():
    subprocess.run(
        ["/usr/local/sbin/naverwebtoon-backend-compose", "up", "-d", "--wait", "--wait-timeout", "180"],
        check=True,
    )


if old == new:
    sys.exit(0)
write(new)
try:
    compose()
    subprocess.run(
        [
            "/usr/bin/docker",
            "exec",
            "naverwebtoon-api-1",
            "python",
            "-c",
            "from app.integrations import get_integrations; "
            "s=get_integrations().storage; s.client.head_bucket(Bucket=s.bucket)",
        ],
        check=True,
    )
except Exception:
    write(old)
    try:
        compose()
    finally:
        sys.exit("설정 적용 실패: 기존 설정으로 되돌렸습니다.")
print("운영 연동 설정을 적용했습니다.")
