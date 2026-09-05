# -*- coding: utf-8 -*-
"""源码断言测试的共享读取器。

巨石文件拆分后（pipeline → agent_flow/notification_flow，analyzer →
decision_stability，main.js → lib/*），读源码文本的断言测试必须同时
覆盖拆分目标文件，否则会静默退化：

- ``assertIn`` 类断言：模式随代码搬走 → 假阴性（断言失效但测试通过）
- ``assertNotIn`` 类断言：对残留文件的检查无法约束新文件 → 保护范围缩水

统一入口 ``read_logical_source("src/core/pipeline.py")`` 返回逻辑模块的
全部物理文件拼接文本（文件间插入 2000 字符分隔，防止断言的近邻窗口
跨文件污染）。拆分/新增文件时在 ``LOGICAL_MODULE_FILES`` 对应条目追加。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SEPARATOR = "\n" * 2000

# 逻辑模块 → 物理文件清单（主文件在前，与拆分批次一致）
LOGICAL_MODULE_FILES: dict[str, tuple[str, ...]] = {
    "src/core/pipeline.py": (
        "src/core/pipeline.py",
        "src/core/agent_flow.py",
        "src/core/notification_flow.py",
    ),
    "src/analyzer.py": (
        "src/analyzer.py",
        "src/decision_stability.py",
    ),
    "apps/dsa-desktop/main.js": (
        "apps/dsa-desktop/main.js",
        "apps/dsa-desktop/lib/app-paths.js",
        "apps/dsa-desktop/lib/logger.js",
        "apps/dsa-desktop/lib/update-core.js",
    ),
}


def read_logical_source(logical_path: str) -> str:
    """读取逻辑模块源码；拆分文件自动拼接（含分隔）。"""
    files = LOGICAL_MODULE_FILES.get(logical_path, (logical_path,))
    texts = []
    for rel in files:
        path = REPO_ROOT / rel
        if not path.is_file():
            raise FileNotFoundError(f"logical source member missing: {rel}")
        texts.append(path.read_text(encoding="utf-8"))
    return SEPARATOR.join(texts) if len(texts) > 1 else texts[0]
