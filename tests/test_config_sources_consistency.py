# -*- coding: utf-8 -*-
"""配置三体一致性守卫：config_registry / .env.example / 代码读取 三方不允许静默漂移。

配置知识分散在三处手工同步（src/core/config_registry.py 的 WebUI 字段注册表、
.env.example 的用户文档、各模块的 os.getenv 读取）。任何一方漏改都是静默漂移：

- registry 新增键但 .env.example 没文档 → 用户不知道该键存在；
- registry 暴露键但代码从不读取 → WebUI 呈现无效开关。

本测试把漂移变成 CI 失败。新增配置键时请同步：registry 注册 + .env.example 文档 +
代码读取（或加入下方对应允许清单并注明理由）。历史背景：2026-09 守卫建立时
曾一次性补齐 16 个"registry 有注册但 .env.example 无文档"的漂移键。
"""

import re
from functools import lru_cache
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]

# 有意不在 .env.example 文档化的 registry 键（必须注明理由）
ENV_EXAMPLE_INTENTIONALLY_UNDOCUMENTED: frozenset[str] = frozenset()

# 无直接 os.getenv 字面量、经动态方式读取的 registry 键（键名作变量/从 .env 解析）：
# - ADMIN_AUTH_ENABLED：src/auth.py 从解析后的 .env 字典读取
# - RUN_IMMEDIATELY / SCHEDULE_ENABLED / SCHEDULE_RUN_IMMEDIATELY：src/config.py 运行时覆盖语义清单
# - DSA_RUNTIME_SCHEDULER_TIMEOUT_SECONDS / WEBUI_AUTO_BUILD：runtime scheduler 动态读取
DYNAMICALLY_READ_KEYS: frozenset[str] = frozenset(
    {
        "ADMIN_AUTH_ENABLED",
        "RUN_IMMEDIATELY",
        "SCHEDULE_ENABLED",
        "SCHEDULE_RUN_IMMEDIATELY",
        "DSA_RUNTIME_SCHEDULER_TIMEOUT_SECONDS",
        "WEBUI_AUTO_BUILD",
    }
)

_ENV_KEY_LINE_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=", re.MULTILINE)
_CODE_ENV_READ_RE = re.compile(
    r"""(?:os\.getenv\(\s*|os\.environ\.get\(\s*|os\.environ\[[\s]*)(['"])([A-Z][A-Z0-9_]+)\1"""
)


@lru_cache(maxsize=1)
def _registered_keys() -> frozenset[str]:
    from src.core.config_registry import get_registered_field_keys

    return frozenset(get_registered_field_keys())


@lru_cache(maxsize=1)
def _env_example_text() -> str:
    return (ROOT_DIR / ".env.example").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _codebase_env_key_reads() -> frozenset[str]:
    """全仓库直接 os.getenv / os.environ 字面量键的并集（不含测试）。"""
    pattern = _CODE_ENV_READ_RE
    reads: set[str] = set()
    for base in ("src", "bot", "api", "data_provider", "scripts"):
        for path in (ROOT_DIR / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            reads |= {m.group(2) for m in pattern.finditer(path.read_text(encoding="utf-8", errors="ignore"))}
    reads |= {m.group(2) for m in pattern.finditer((ROOT_DIR / "main.py").read_text(encoding="utf-8", errors="ignore"))}
    return frozenset(reads)


def test_registered_keys_are_canonical_naming() -> None:
    bad = [key for key in _registered_keys() if not re.fullmatch(r"[A-Z][A-Z0-9_]+", key)]
    assert not bad, f"registry 键必须为大写下划线形式: {bad}"


def test_every_registered_key_is_documented_in_env_example() -> None:
    env_text = _env_example_text()
    missing = sorted(
        key
        for key in _registered_keys()
        if key not in env_text and key not in ENV_EXAMPLE_INTENTIONALLY_UNDOCUMENTED
    )
    assert not missing, (
        "以下 registry 键在 .env.example 中完全没有文档（用户无法发现这些配置）。"
        "请在 .env.example 对应分区补文档，或确认该键确属内部键并加入"
        " ENV_EXAMPLE_INTENTIONALLY_UNDOCUMENTED 允许清单（注明理由）：\n" + "\n".join(missing)
    )


def test_every_registered_key_is_read_by_codebase() -> None:
    unread = sorted(
        (set(_registered_keys()) - set(_codebase_env_key_reads())) - DYNAMICALLY_READ_KEYS
    )
    assert not unread, (
        "以下 registry 键没有任何代码直接读取（WebUI 暴露了无效开关）。"
        "请实现读取逻辑，或确认属动态读取并加入 DYNAMICALLY_READ_KEYS 允许清单：\n"
        + "\n".join(unread)
    )


@pytest.mark.parametrize("key", sorted(DYNAMICALLY_READ_KEYS))
def test_dynamically_read_keys_are_actually_registered(key: str) -> None:
    """允许清单里的键必须真的在 registry 中——防止清单变成垃圾抽屉。"""
    assert key in _registered_keys(), f"{key} 在 DYNAMICALLY_READ_KEYS 中但未在 registry 注册"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
