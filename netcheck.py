"""공개 주소가 안 열릴 때 원인을 알려 주는 진단 도구.

사용법:  python netcheck.py https://xxxx.trycloudflare.com
(서버가 켜져 있는 PC 와 접속이 안 되는 PC, 두 곳에서 각각 실행해 결과를 비교해 보세요.)
"""
import sys

from server import netdiag


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    try:
        verdict = netdiag.run(argv[1])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    return 0 if verdict.code == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
