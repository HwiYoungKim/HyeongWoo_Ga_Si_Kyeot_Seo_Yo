"""백업 거울 복사 테스트."""
import pytest

from server.backup import BackupError, BackupMirror

IMG = "A" * 16
IMG2 = "B" * 16


@pytest.fixture
def dirs(tmp_path):
    src = tmp_path / "data"
    (src / "rooms").mkdir(parents=True)
    (src / "images").mkdir()
    dst = tmp_path / "drive"
    return src, dst


def test_copies_updates_and_deletes(dirs):
    src, dst = dirs
    (src / "rooms" / "abcdef12.json").write_text("{}")
    (src / "roster.json").write_text("{}")
    (src / "images" / f"{IMG}.webp").write_bytes(b"x")
    m = BackupMirror(src, dst)
    assert m.sync_once() == (3, 0)
    assert (dst / "rooms" / "abcdef12.json").exists() and (dst / "roster.json").exists()
    assert m.sync_once() == (0, 0)                              # 변경 없음
    (src / "roster.json").write_text('{"a":1}')
    assert m.sync_once() == (1, 0)
    (src / "images" / f"{IMG}.webp").unlink()                   # 이미지를 지우면 백업에서도 지워진다
    assert m.sync_once() == (0, 1)
    assert not (dst / "images" / f"{IMG}.webp").exists()


def test_ignores_foreign_files_and_never_deletes_them(dirs):
    src, dst = dirs
    (src / "rooms" / "notes.txt").write_text("x")               # 이름 규칙 밖 -> 복사 안 함
    (src / "images" / "evil.html").write_text("x")
    (src / "secret.txt").write_text("x")
    m = BackupMirror(src, dst)
    (dst / "images").mkdir(exist_ok=True)
    (dst / "images" / "my-own-file.txt").write_text("mine")     # 사용자가 둔 파일은 건드리지 않는다
    (dst / "other.txt").write_text("mine")
    assert m.sync_once() == (0, 0)
    assert (dst / "images" / "my-own-file.txt").exists() and (dst / "other.txt").exists()
    assert not (dst / "rooms" / "notes.txt").exists() and not (dst / "secret.txt").exists()


def test_roster_removed_from_backup_when_source_gone(dirs):
    src, dst = dirs
    (src / "roster.json").write_text("{}")
    m = BackupMirror(src, dst)
    m.sync_once()
    (src / "roster.json").unlink()
    assert m.sync_once() == (0, 1) and not (dst / "roster.json").exists()


def test_refuses_overlapping_folders(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    for bad in (src, src / "backup", tmp_path):
        with pytest.raises(BackupError):
            BackupMirror(src, bad)


def test_symlinks_are_not_followed(dirs, tmp_path):
    src, dst = dirs
    secret = tmp_path / "outside.txt"
    secret.write_text("top secret")
    (src / "images" / f"{IMG2}.webp").symlink_to(secret)
    m = BackupMirror(src, dst)
    assert m.sync_once() == (0, 0)
