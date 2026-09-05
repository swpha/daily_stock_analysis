#!/usr/bin/env python3
"""导出 FastAPI OpenAPI schema 供前端生成 TypeScript 类型。

用法（在项目根目录、已激活含依赖的 Python 环境时）：
    python scripts/dump_openapi.py

输出 apps/dsa-web/openapi.json，随后在 apps/dsa-web 运行：
    npm run generate:api-types
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "apps" / "dsa-web" / "openapi.json"


def main() -> int:
    warnings.filterwarnings("ignore")
    # 保证仓库根在 sys.path（以任意 cwd 调用都可运行）
    sys.path.insert(0, str(REPO_ROOT))

    from api.app import create_app

    app = create_app(static_dir=None)
    schema = app.openapi()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"openapi.json written: {len(schema['paths'])} paths, "
        f"{len(schema.get('components', {}).get('schemas', {}))} schemas -> {OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
