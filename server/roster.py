"""영구 선수 명부.

방(경매)과 무관하게 남는 선수 프로필 저장소다. 방이 삭제되거나 보존 기간이 지나 정리돼도
명부는 그대로 남고, 다음 경매에서 불러와 쓸 수 있다.

- 파일 하나(data/roster.json). 원자적 저장(임시 파일 → fsync → replace), 권한 0600.
- 키는 선수 uid (서버가 부여하는 무작위 16자리 16진수).
- 경매 결과(낙찰가/소속팀)는 저장하지 않는다. 프로필만 저장한다.
- 삭제는 주최자가 명시적으로 할 때만 일어난다.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config
from .validators import is_valid_uid, legacy_roles

log = logging.getLogger("auction.roster")

_PROFILE_KEYS = ("name", "battletag", "roles", "heroes", "images", "elo", "intro")


def new_uid() -> str:
    return secrets.token_hex(8)


class RosterStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "roster.json"
        self._lock = threading.Lock()
        self._players: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ 입출력
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            players = data.get("players", {})
            if isinstance(players, dict):
                self._players = {k: v for k, v in players.items() if is_valid_uid(k) and isinstance(v, dict)}
                for profile in self._players.values():      # 포지션이 하나뿐이던 시절의 명부를 roles 로 옮긴다
                    if "roles" not in profile:
                        profile["roles"] = legacy_roles(profile.get("role"))
                    profile.pop("role", None)
        except (OSError, ValueError):
            log.warning("선수 명부 파일을 읽을 수 없습니다. 원본은 건드리지 않고 비어 있는 것으로 시작합니다.")
            # 손상된 파일을 덮어써서 잃지 않도록 옆에 보관해 둔다.
            try:
                os.replace(self.path, self.path.with_suffix(f".corrupt-{int(time.time())}"))
            except OSError:
                pass

    def _save_locked(self) -> None:
        payload = {"version": 1, "players": self._players}
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".json")
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ 조회
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [{"uid": uid, **p} for uid, p in self._players.items()]
        items.sort(key=lambda p: (p.get("name", "").casefold(), p["uid"]))
        return items

    def get(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            p = self._players.get(uid)
            return {"uid": uid, **p} if p else None

    def count(self) -> int:
        with self._lock:
            return len(self._players)

    def image_ids(self) -> set[str]:
        with self._lock:
            return {iid for p in self._players.values() for iid in p.get("images", [])}

    # ------------------------------------------------------------------ 변경
    def upsert_many(self, players: Iterable[dict[str, Any]]) -> None:
        """검증이 끝난 선수 프로필들을 저장한다. uid 가 이미 있으면 덮어쓴다."""
        with self._lock:
            updated = dict(self._players)
            now = time.time()
            for p in players:
                uid = p["uid"]
                if not is_valid_uid(uid):
                    continue
                updated[uid] = {k: p[k] for k in _PROFILE_KEYS} | {"updated_at": now}
            if len(updated) > config.MAX_ROSTER:
                from .engine import AuctionError
                raise AuctionError("roster_full", f"선수 명부는 최대 {config.MAX_ROSTER}명까지 저장할 수 있습니다. 안 쓰는 선수를 삭제해 주세요.")
            self._players = updated
            self._save_locked()

    def delete(self, uid: str) -> bool:
        with self._lock:
            if uid not in self._players:
                return False
            updated = dict(self._players)
            del updated[uid]
            self._players = updated
            self._save_locked()
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._players)
            self._players = {}
            self._save_locked()
            return n
