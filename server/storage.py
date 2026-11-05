"""방 상태의 파일 저장소.

- 방 하나당 JSON 파일 하나 (data/rooms/<room_id>.json).
- 원자적 저장: 임시 파일에 쓰고 fsync 후 os.replace. 저장 도중 꺼져도 파일이 깨지지 않는다.
- 권한: 폴더 0700, 파일 0600 (POSIX). 같은 PC의 다른 계정이 선수 정보를 읽지 못하게 한다.
  (Windows 는 POSIX 권한 모델이 달라 효과가 제한적이므로 사용자 프로필 폴더 사용을 권장한다.)
- room_id 는 정규식으로 검증한 뒤에만 경로에 사용한다 (경로 조작 방지).
- 보존 기간이 지난 방은 자동 삭제한다 (개인정보 최소 보관).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import tempfile
import threading
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
        self.save_text(room_id, json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def save_text(self, room_id: str, text: str) -> None:
        path = self._path(room_id)
        fd, tmp_name = tempfile.mkstemp(dir=self.rooms_dir, prefix=".tmp-", suffix=".json")
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
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


class WriteBehind:
    """디스크 저장을 입찰 처리 경로 밖으로 뺀다.

    fsync 는 윈도우/외장 드라이브에서 수십 ms 가 걸릴 수 있다. 방 락을 쥔 채로 기다리면 그동안
    모든 입찰이 줄을 선다. 그래서 (1) 락 안에서는 JSON 문자열만 만들어 맡기고
    (2) 별도 스레드가 같은 방의 연속 저장을 하나로 묶어(delay 초) 파일에 쓴다.
    프로그램을 정상 종료하거나 flush() 를 부르면 대기 중인 것을 모두 즉시 쓴다.
    """

    def __init__(self, store: RoomStore, delay: float = 0.25):
        self.store = store
        self.delay = delay
        self._pending: dict[str, tuple[float, str]] = {}     # room_id -> (쓸 시각, JSON)
        self._cond = threading.Condition()
        self._io = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="room-writer", daemon=True)
        self._thread.start()
        atexit.register(self.flush)

    def submit(self, room_id: str, text: str) -> None:
        with self._cond:
            due = self._pending[room_id][0] if room_id in self._pending else time.monotonic() + self.delay
            self._pending[room_id] = (due, text)        # 같은 방의 이전 대기분은 최신으로 교체
            self._cond.notify()

    def discard(self, room_id: str) -> None:
        """방 삭제 시: 대기 중인 저장을 버리고, 쓰는 중이면 끝날 때까지 기다린다 (삭제한 파일이 되살아나지 않게)."""
        with self._cond:
            self._pending.pop(room_id, None)
        with self._io:
            pass

    def flush(self) -> None:
        with self._cond:
            items = list(self._pending.items())
            self._pending.clear()
        for room_id, (_due, text) in items:
            self._write(room_id, text)

    def close(self) -> None:
        self.flush()
        with self._cond:
            self._stop = True
            self._cond.notify()

    def _write(self, room_id: str, text: str) -> None:
        with self._io:
            try:
                self.store.save_text(room_id, text)
            except OSError:
                log.exception("방 저장 실패: %s", room_id)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._stop:
                    if self._pending:
                        wait = min(d for d, _ in self._pending.values()) - time.monotonic()
                        if wait <= 0:
                            break
                        self._cond.wait(wait)
                    else:
                        self._cond.wait()
                if self._stop:
                    return
                now = time.monotonic()
                ready = [(rid, t) for rid, (d, t) in self._pending.items() if d <= now]
                for rid, _ in ready:
                    self._pending.pop(rid, None)
            for rid, text in ready:
                self._write(rid, text)
