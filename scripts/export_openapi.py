"""OpenAPI 스키마를 docs/openapi.json으로 내보낸다.

python scripts/export_openapi.py          # 파일 갱신
python scripts/export_openapi.py --check  # 파일이 최신인지 확인 (다르면 종료 코드 1)
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "development")

from app.main import create_app  # noqa: E402

OUTPUT = ROOT / "docs" / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print("docs/openapi.json이 최신이 아닙니다. scripts/export_openapi.py를 실행하세요.")
            return 1
        return 0
    OUTPUT.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
