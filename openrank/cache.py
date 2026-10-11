"""Filesystem cache manager with XDG path resolution, atomic writes, and TTL support."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union


class CacheManager:
    """Manages local JSON cache files with TTL guarantees and atomic writes."""

    def __init__(
        self,
        cache_dir: Optional[Union[str, Path]] = None,
        default_ttl: int = 86400,
    ) -> None:
        self.default_ttl = default_ttl
        self._in_memory_only = False
        self._mem_cache: Dict[str, Dict[str, Any]] = {}

        if cache_dir is not None:
            self.cache_dir = Path(cache_dir).expanduser().resolve()
            self._ensure_cache_dir(self.cache_dir)
        else:
            self.cache_dir = self._resolve_xdg_cache_dir()

    def _ensure_cache_dir(self, directory: Path) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            test_file = directory / ".write_test"
            test_file.touch()
            test_file.unlink()
        except (PermissionError, OSError):
            fallback = Path(tempfile.gettempdir()) / "openrank_cache"
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                test_file = fallback / ".write_test"
                test_file.touch()
                test_file.unlink()
                self.cache_dir = fallback
            except (PermissionError, OSError):
                self._in_memory_only = True

    def _resolve_xdg_cache_dir(self) -> Path:
        xdg_env = os.environ.get("XDG_CACHE_HOME")
        if xdg_env and xdg_env.strip():
            candidate = Path(xdg_env).expanduser() / "openrank"
        else:
            candidate = Path.home() / ".cache" / "openrank"

        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_file = candidate / ".write_test"
            test_file.touch()
            test_file.unlink()
            return candidate
        except (PermissionError, OSError):
            fallback = Path(tempfile.gettempdir()) / "openrank_cache"
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                test_file = fallback / ".write_test"
                test_file.touch()
                test_file.unlink()
                return fallback
            except (PermissionError, OSError):
                self._in_memory_only = True
                return candidate

    def _key_to_path(self, key: str) -> Path:
        safe_key = re.sub(r"[^a-zA-Z0-9_\.]", "_", key.replace("-", "_"))
        if not safe_key.endswith(".json"):
            safe_key += ".json"
        return self.cache_dir / safe_key

    def set(self, key: str, data: Any, ttl: Optional[int] = None) -> None:
        eff_ttl = self.default_ttl if ttl is None else ttl
        now = time.time()
        expires_at = (now + eff_ttl) if (eff_ttl is not None and eff_ttl > 0) else 0.0

        envelope = {
            "_metadata": {
                "key": key,
                "created_at": now,
                "ttl": eff_ttl,
                "expires_at": expires_at,
            },
            "data": data,
        }

        if self._in_memory_only:
            self._mem_cache[key] = envelope
            return

        target_path = self._key_to_path(key)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.cache_dir,
            prefix=f".{target_path.stem}_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def get(self, key: str, ignore_ttl: bool = False) -> Optional[Any]:
        if self._in_memory_only:
            envelope = self._mem_cache.get(key)
            if not envelope or not isinstance(envelope, dict):
                return None
            meta = envelope.get("_metadata")
            exp = meta.get("expires_at") if isinstance(meta, dict) else 0.0
            if not ignore_ttl and isinstance(exp, (int, float)) and exp > 0.0 and time.time() > exp:
                return None
            return envelope.get("data")

        target_path = self._key_to_path(key)
        if not target_path.is_file():
            return None

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            return None

        if not isinstance(envelope, dict) or "data" not in envelope:
            return None

        meta = envelope.get("_metadata")
        exp = meta.get("expires_at") if isinstance(meta, dict) else 0.0
        if not ignore_ttl and isinstance(exp, (int, float)) and exp > 0.0 and time.time() > exp:
            return None

        return envelope.get("data")

    def is_valid(self, key: str) -> bool:
        return self.get(key, ignore_ttl=False) is not None

    def has(self, key: str, valid_only: bool = True) -> bool:
        if valid_only:
            return self.is_valid(key)
        if self._in_memory_only:
            return key in self._mem_cache
        return self._key_to_path(key).is_file()

    def get_metadata(self, key: str) -> Optional[Dict[str, Any]]:
        if self._in_memory_only:
            env = self._mem_cache.get(key)
            return env.get("_metadata") if env else None
        target_path = self._key_to_path(key)
        if not target_path.is_file():
            return None
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                env = json.load(f)
            return env.get("_metadata") if isinstance(env, dict) else None
        except Exception:
            return None

    def delete(self, key: str) -> bool:
        if self._in_memory_only:
            return self._mem_cache.pop(key, None) is not None
        target_path = self._key_to_path(key)
        if target_path.is_file():
            try:
                target_path.unlink()
                return True
            except OSError:
                return False
        return False

    def clear(self) -> int:
        if self._in_memory_only:
            count = len(self._mem_cache)
            self._mem_cache.clear()
            return count

        count = 0
        if self.cache_dir.is_dir():
            for p in self.cache_dir.glob("*.json"):
                try:
                    p.unlink()
                    count += 1
                except OSError:
                    pass
        return count
