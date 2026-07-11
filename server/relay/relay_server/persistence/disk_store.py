from __future__ import annotations

import time
from pathlib import Path


class DiskBlockStore:
    """将数据块写入磁盘；路径由关系库记录。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def initialize(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def disk_path_for_token(self, token: str) -> Path:
        shard = token[:2]
        return self._data_dir / shard / f"{token}.bin"

    def relative_disk_path(self, token: str) -> str:
        return str(self.disk_path_for_token(token).relative_to(self._data_dir))

    async def write(self, token: str, data: bytes) -> Path:
        path = self.disk_path_for_token(token)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    async def read(self, disk_path: str) -> bytes | None:
        path = self._data_dir / disk_path
        if not path.is_file():
            return None
        return path.read_bytes()

    async def remove(self, disk_path: str) -> None:
        path = self._data_dir / disk_path
        if path.is_file():
            path.unlink()
        self._prune_empty_parent(path.parent)

    async def sweep_orphan_files(
        self,
        *,
        active_relative_paths: set[str],
        max_age_seconds: int,
    ) -> int:
        """删除无 DB 行且超过 max_age 的磁盘 blob（按文件 mtime）。"""
        cutoff = time.time() - max_age_seconds
        removed = 0
        if not self._data_dir.is_dir():
            return 0

        # 先快照再删除，避免 Windows rglob 在目录被删后继续扫描时报错。
        for path in list(self._data_dir.rglob("*.bin")):
            try:
                if not path.is_file():
                    continue
                relative = str(path.relative_to(self._data_dir))
                if relative in active_relative_paths:
                    continue
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
            self._prune_empty_parent(path.parent)
        return removed

    @staticmethod
    def _prune_empty_parent(directory: Path) -> None:
        if not directory.is_dir():
            return
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
