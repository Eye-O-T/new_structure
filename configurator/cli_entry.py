# 패키징된 CLI 실행 파일의 시작점이다. 실제 명령 처리는 cli 모듈로 위임한다.

"""Console entry point used by the frozen Windows CLI executable."""

from configurator.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
