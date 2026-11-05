"""데이터 폴더를 다른 폴더(예: Google Drive for Desktop 동기화 폴더)로 거울처럼 복사한다.

구글 드라이브 API 를 쓰지 않는다. 이 프로그램은 로컬 폴더에 복사만 하고, 그 폴더를 구글 드라이브로
올리는 일은 PC 에 설치된 Google Drive for Desktop 앱이 한다. (그래서 계정 연동/키 발급이 필요 없다.)

- 복사 대상: rooms/<방id>.json, roster.json, images/<이미지id>.webp  (이 이름 규칙에 맞는 것만)
- 원본에서 사라진 파일은 백업 쪽에서도 지운다 (개인정보 최소 보관: 이미지를 지우면 백업도 지워진다).
  단, 위 이름 규칙에 맞지 않는 파일은 절대 건드리지 않는다.
- 변경이 없으면 아무것도 하지 않는다 (수정 시각+크기 비교). 임시 이름으로 쓴 뒤 교체해서 반쯤 쓰인 파일이 올라가지 않는다.
- 백업 폴더가 원본 안에 있거나 원본을 포함하면 시작을 거부한다 (무한 복사/덮어쓰기 방지).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

from .validators import IMAGE_ID_RE, ROOM_ID_RE

log = logging.getLogger("auction.backup")


class BackupError(Exception):
    pass


def _wanted(rel: Path) -> bool:
    parts = rel.parts
    if parts == ("roster.json",):
        return True
    if len(parts) == 2 and parts[0] == "rooms" and rel.suffix == ".json":
        return bool(ROOM_ID_RE.match(rel.stem))
    if len(parts) == 2 and parts[0] == "images" and rel.suffix == ".webp":
        return bool(IMAGE_ID_RE.match(rel.stem))
    return False


class BackupMirror:
    def __init__(self, source: str | os.PathLike[str], dest: str | os.PathLike[str], interval_sec: float = 10.0):
        self.source = Path(source).resolve()
        self.dest = Path(dest).expanduser().resolve()
        self.interval = max(2.0, interval_sec)
        if self.dest == self.source or self.source in self.dest.parents or self.dest in self.source.parents:
            raise BackupError("백업 폴더는 데이터 폴더와 겹치면 안 됩니다. 다른 폴더를 지정해 주세요.")
        self.dest.mkdir(parents=True, exist_ok=True)
        if not os.access(self.dest, os.W_OK):
            raise BackupError("백업 폴더에 쓸 수 없습니다.")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 한 번 동기화
    def _source_files(self) -> dict[Path, Path]:
        out: dict[Path, Path] = {}
        for sub in ("rooms", "images"):
            for path in (self.source / sub).glob("*"):
                rel = path.relative_to(self.source)
                if path.is_file() and not path.is_symlink() and _wanted(rel):
                    out[rel] = path
        roster = self.source / "roster.json"
        if roster.is_file() and not roster.is_symlink():
            out[Path("roster.json")] = roster
        return out

    def sync_once(self) -> tuple[int, int]:
        """(복사한 파일 수, 지운 파일 수)"""
        copied = removed = 0
        files = self._source_files()
        for rel, src in files.items():
            dst = self.dest / rel
            try:
                s = src.stat()
                if dst.exists():
                    d = dst.stat()
                    if d.st_size == s.st_size and d.st_mtime >= s.st_mtime:
                        continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(f".{dst.name}.part")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
                os.utime(dst, (s.st_atime, s.st_mtime))
                copied += 1
            except OSError:
                log.warning("백업 복사 실패: %s", rel)
        for sub in ("rooms", "images"):
            for path in (self.dest / sub).glob("*") if (self.dest / sub).is_dir() else []:
                rel = path.relative_to(self.dest)
                if path.is_file() and _wanted(rel) and rel not in files:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        log.warning("백업 삭제 실패: %s", rel)
        roster = self.dest / "roster.json"
        if roster.is_file() and Path("roster.json") not in files:
            try:
                roster.unlink()
                removed += 1
            except OSError:
                pass
        if copied or removed:
            log.info("백업: %d개 복사, %d개 삭제 -> %s", copied, removed, self.dest)
        return copied, removed

    # ------------------------------------------------------------------ 주기 실행
    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    self.sync_once()
                except Exception:
                    log.exception("백업 중 오류")
        self.sync_once()
        self._thread = threading.Thread(target=loop, name="backup-mirror", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sync_once()        # 종료 직전 마지막 동기화
        except Exception:
            log.exception("마지막 백업 중 오류")
