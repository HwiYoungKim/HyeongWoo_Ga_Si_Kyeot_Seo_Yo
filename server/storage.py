"""방 상태의 파일 저장소.

- 방 하나당 JSON 파일 하나 (data/rooms/<room_id>.json).
- 원자적 저장: 임시 파일에 쓰고 fsync 후 os.replace. 저장 도중 꺼져도 파일이 깨지지 않는다.
- 권한: 폴더 0700, 파일 0600 (POSIX). 같은 PC의 다른 계정이 선수 정보를 읽지 못하게 한다.
  (Windows 는 POSIX 권한 모델이 달라 효과가 제한적이므로 사용자 프로필 폴더 사용을 권장한다.)
- room_id 는 정규식으로 검증한 뒤에만 경로에 사용한다 (경로 조작 방지).
- 보존 기간이 지난 방은 자동 삭제한다 (개인정보 최소 보관).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .validators import ROOM_ID_RE

log = logging.getLogger("auction.storage")


class RoomStore:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.root = Path(data_dir).resolve()
        self.rooms_dir = self.root / "rooms"
        self._ensure_dir(self.root)
        self._ensure_dir(self.rooms_dir)

    @staticmethod
    def _ensure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def _path(self, room_id: str) -> Path:
        if not ROOM_ID_RE.match(room_id):
            raise ValueError("invalid room id")
        return self.rooms_dir / f"{room_id}.json"

    def save(self, room_id: str, data: dict[str, Any]) -> None:
        path = self._path(room_id)
        fd, tmp_name = tempfile.mkstemp(dir=self.rooms_dir, prefix=".tmp-", suffix=".json")
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def delete(self, room_id: str) -> None:
        try:
            self._path(room_id).unlink()
        except FileNotFoundError:
            pass

    def load_all(self) -> Iterator[dict[str, Any]]:
        for path in sorted(self.rooms_dir.glob("*.json")):
            if not ROOM_ID_RE.match(path.stem):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    yield json.load(f)
            except (OSError, ValueError):
                log.warning("손상된 방 파일을 건너뜁니다: %s", path.name)

    def purge_older_than(self, days: float) -> list[str]:
        """마지막 갱신으로부터 days 일이 지난 파일을 삭제하고 삭제된 room_id 목록을 돌려준다."""
        if days <= 0:
            return []
        cutoff = time.time() - days * 86400
        removed = []
        for path in list(self.rooms_dir.glob("*.json")):
            try:
                if path.stat().st_mtime < cutoff and ROOM_ID_RE.match(path.stem):
                    path.unlink()
                    removed.append(path.stem)
            except OSError:
                continue
        return removed
