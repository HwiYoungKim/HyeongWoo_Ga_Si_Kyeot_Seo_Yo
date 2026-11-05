#!/usr/bin/env python3
"""라이브 선수 경매 서버 — 실행·관리 진입점.

개인 PC에서 서버를 열어 쓰는 용도로 만들었다.

  python app.py                  # 내 PC에서만 접속 가능 (127.0.0.1) — 가장 안전한 기본값
  python app.py --lan            # 같은 와이파이/공유기의 다른 사람도 접속 (0.0.0.0)
  python app.py --public-url https://xxxx.trycloudflare.com   # 터널로 외부에 공개할 때 링크에 쓸 주소

관리 명령 (서버를 띄우지 않고 저장된 방을 관리하고 종료)
  python app.py --list-rooms             # 저장된 방 목록
  python app.py --delete-room <방ID>     # 방 하나 삭제 (선수 정보 포함)
  python app.py --purge-days 7           # 7일 넘게 갱신되지 않은 방 모두 삭제
  python app.py --list-roster            # 영구 선수 명부 목록
  python app.py --wipe-roster            # 선수 명부와 이미지를 모두 삭제

자세한 보안 안내는 README.md 를 참고한다.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

from server import config
from server.app_config import AppConfig
from server.app_factory import configure_logging, create_app
from server.netutil import lan_ips
from server.storage import RoomStore
from server.validators import ROOM_ID_RE


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="라이브 선수 경매 서버",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    net = p.add_argument_group("네트워크")
    net.add_argument("--host", default="127.0.0.1",
                     help="바인드 주소 (기본 127.0.0.1 = 내 PC에서만 접속)")
    net.add_argument("--lan", action="store_true",
                     help="같은 네트워크의 다른 기기도 접속하도록 0.0.0.0 으로 연다")
    net.add_argument("--port", type=int, default=5000, help="포트 (기본 5000)")
    net.add_argument("--public-url", default=None,
                     help="터널/도메인 등 외부 공개 주소. 초대 링크의 기본 주소로 쓰인다")
    net.add_argument("--trust-proxy", action="store_true",
                     help="리버스 프록시/터널 뒤에서 실행할 때만 켠다 (X-Forwarded-* 신뢰)")
    net.add_argument("--cert", default=None, help="HTTPS 인증서 파일(PEM)")
    net.add_argument("--key", default=None, help="HTTPS 개인키 파일(PEM)")

    sec = p.add_argument_group("보안/데이터")
    sec.add_argument("--admin-key", default=os.environ.get("AUCTION_ADMIN_KEY"),
                     help="방 생성에 필요한 관리자 키. 생략하면 실행할 때마다 새로 만든다 "
                          "(환경변수 AUCTION_ADMIN_KEY 로도 지정 가능)")
    sec.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"),
                     help="방 데이터 저장 폴더 (기본: ./data, git 에 올라가지 않는다)")
    sec.add_argument("--retention-days", type=float, default=config.DEFAULT_RETENTION_DAYS,
                     help=f"마지막 사용 후 N일이 지난 방을 자동 삭제 (기본 {config.DEFAULT_RETENTION_DAYS}, 0 이면 끔)")

    bk = p.add_argument_group("백업")
    bk.add_argument("--backup-dir", metavar="PATH",
                    help="데이터(방·선수 명부·이미지)를 이 폴더로 자동 복사한다. Google Drive for Desktop 의 "
                         "동기화 폴더를 지정하면 드라이브에도 올라간다. (기본: 끔)")
    bk.add_argument("--backup-interval", type=float, default=10.0, metavar="SEC",
                    help="백업 확인 주기(초, 기본 10)")

    misc = p.add_argument_group("기타")
    misc.add_argument("--no-browser", action="store_true", help="시작할 때 브라우저를 자동으로 열지 않는다")
    misc.add_argument("--verbose", action="store_true", help="자세한 로그")

    adm = p.add_argument_group("관리 명령 (실행 후 바로 종료)")
    adm.add_argument("--list-rooms", action="store_true", help="저장된 방 목록 출력")
    adm.add_argument("--delete-room", metavar="ROOM_ID", help="방 하나를 삭제")
    adm.add_argument("--purge-days", type=float, metavar="DAYS", help="DAYS일 넘게 갱신 안 된 방 전부 삭제")
    adm.add_argument("--list-roster", action="store_true", help="영구 선수 명부 목록 출력")
    adm.add_argument("--wipe-roster", action="store_true",
                     help="영구 선수 명부와 연결된 이미지를 모두 삭제 (확인 문구 입력 필요)")
    return p


def run_admin_command(args: argparse.Namespace) -> bool:
    """관리 명령이 지정되었으면 실행하고 True 를 반환한다."""
    if not (args.list_rooms or args.delete_room or args.purge_days is not None
            or args.list_roster or args.wipe_roster):
        return False
    store = RoomStore(args.data_dir)
    if args.list_rooms:
        rows = list(store.load_all())
        if not rows:
            print("저장된 방이 없습니다.")
        for d in sorted(rows, key=lambda r: r.get("updated_at", 0)):
            updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("updated_at", 0)))
            print(f"{d.get('id')}  {d.get('state'):<15} 팀 {d.get('num_teams')}  "
                  f"매물 {len(d.get('players', [])):>2}명  갱신 {updated}  {d.get('title')}")
    if args.delete_room:
        if not ROOM_ID_RE.match(args.delete_room):
            print("방 ID 형식이 올바르지 않습니다.", file=sys.stderr)
            sys.exit(2)
        store.delete(args.delete_room)
        print(f"삭제했습니다: {args.delete_room}")
    if args.purge_days is not None:
        removed = store.purge_older_than(args.purge_days)
        print(f"{len(removed)}개 방을 삭제했습니다.")
    if args.delete_room or args.purge_days is not None or args.wipe_roster or args.list_roster:
        from server.manager import RoomManager
        manager = RoomManager(store)
        manager.load_from_disk()
        if args.list_roster:
            players = manager.roster.list()
            if not players:
                print("선수 명부가 비어 있습니다.")
            for pl in players:
                print(f"{pl['uid']}  {pl.get('role') or '-':<8} {pl.get('name')}  {pl.get('battletag') or ''}"
                      f"  이미지 {len(pl.get('images', []))}장")
        if args.wipe_roster:
            typed = input(f"선수 명부 {manager.roster.count()}명과 이미지를 모두 삭제합니다. 계속하려면 DELETE 를 입력: ")
            if typed.strip() == "DELETE":
                n = manager.roster.clear()
                manager.gc_images(grace_sec=0)
                print(f"명부 {n}명을 삭제했습니다.")
            else:
                print("취소했습니다.")
        elif args.delete_room or args.purge_days is not None:
            n = manager.gc_images()
            if n:
                print(f"연결되지 않은 이미지 {n}개를 정리했습니다.")
    return True


def print_banner(cfg: AppConfig, scheme: str, key_is_generated: bool) -> None:
    local = f"{scheme}://127.0.0.1:{cfg.port}"
    line = "=" * 64
    print(line)
    print(" 라이브 선수 경매 서버")
    print(line)
    print(f" 내 PC에서 열기 : {local}")
    if cfg.lan_enabled:
        for ip in cfg.lan_ips:
            print(f" 같은 네트워크  : {scheme}://{ip}:{cfg.port}")
    if cfg.public_url:
        print(f" 외부 공개 주소 : {cfg.public_url}")
    print(f" 관리자 키      : {cfg.admin_key}" + ("   (이번 실행에서만 유효)" if key_is_generated else ""))
    print(f" 데이터 폴더    : {cfg.data_dir}   (선수 명부·이미지 포함, git 에 올라가지 않음)")
    print(f" 보존 기간      : {'무제한' if cfg.retention_days <= 0 else f'{cfg.retention_days:g}일'}")
    print(line)
    if cfg.lan_enabled and not cfg.secure and not cfg.public_url:
        print(" [주의] HTTP(암호화 없음)입니다. 선수 정보가 오가므로 신뢰할 수 있는")
        print("        네트워크(집/팀 공유기)에서만 쓰고, 인터넷에는 직접 열지 마세요.")
        print("        외부 공개가 필요하면 HTTPS 터널(예: Cloudflare Tunnel)을 쓰세요.")
    print(" 종료: Ctrl + C   (진행 중인 경매는 저장되며 다음 실행 때 일시정지 상태로 복구됩니다)")
    print(line)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")      # type: ignore[attr-defined]
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    if run_admin_command(args):
        return 0

    if not (1 <= args.port <= 65535):
        print("포트 번호가 올바르지 않습니다.", file=sys.stderr)
        return 2
    if bool(args.cert) != bool(args.key):
        print("--cert 와 --key 는 함께 지정해야 합니다.", file=sys.stderr)
        return 2
    ssl_context = None
    if args.cert:
        for f in (args.cert, args.key):
            if not Path(f).is_file():
                print(f"파일을 찾을 수 없습니다: {f}", file=sys.stderr)
                return 2
        ssl_context = (args.cert, args.key)

    key_generated = not args.admin_key
    admin_key = args.admin_key or secrets.token_urlsafe(12)
    if len(admin_key) < 8:
        print("관리자 키는 8자 이상이어야 합니다.", file=sys.stderr)
        return 2

    host = "0.0.0.0" if args.lan else args.host
    scheme = "https" if ssl_context else "http"
    public_url = args.public_url
    cfg = AppConfig(
        data_dir=args.data_dir,
        admin_key=admin_key,
        host=host,
        port=args.port,
        public_url=public_url,
        lan_ips=lan_ips() if host not in ("127.0.0.1", "localhost", "::1") else [],
        retention_days=args.retention_days,
        trust_proxy=args.trust_proxy,
        secure=bool(ssl_context) or bool(public_url and public_url.startswith("https://")),
    )

    configure_logging(args.verbose)
    # 응답 헤더의 Server 값으로 Werkzeug/Python 버전이 노출되지 않게 한다.
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.server_version = "Auction"
    WSGIRequestHandler.sys_version = ""
    auction = create_app(cfg)
    auction.manager.gc_images()           # 시작할 때 연결되지 않은 오래된 이미지 정리
    mirror = None
    if args.backup_dir:
        from server.backup import BackupError, BackupMirror
        try:
            mirror = BackupMirror(cfg.data_dir, args.backup_dir, args.backup_interval)
        except BackupError as e:
            print(f"백업을 시작할 수 없습니다: {e}", file=sys.stderr)
            return 2
        mirror.start()
        print(f" 자동 백업      : {mirror.dest}  ({mirror.interval:g}초마다 확인)")
    auction.start_background()
    print_banner(cfg, scheme, key_generated)

    if not args.no_browser:
        url = f"{scheme}://127.0.0.1:{cfg.port}/#admin={admin_key}"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    run_kwargs = {"ssl_context": ssl_context} if ssl_context else {}
    try:
        auction.socketio.run(
            auction.flask, host=host, port=cfg.port,
            debug=False, use_reloader=False, allow_unsafe_werkzeug=True, **run_kwargs,
        )
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"서버를 시작할 수 없습니다: {e}\n(포트 {cfg.port} 가 이미 사용 중이면 --port 로 다른 번호를 지정하세요.)",
              file=sys.stderr)
        return 1
    if mirror is not None:
        mirror.stop()
    print("\n서버를 종료합니다. 방 데이터는 data 폴더에 저장되어 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
