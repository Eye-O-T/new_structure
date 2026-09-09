# 패키징된 CLI 실행 파일의 시작점이다. 실제 명령 처리는 cli 모듈로 위임한다.

from server.setup.install_helper.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
