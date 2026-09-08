# 패키징된 GUI 실행 파일의 시작점이다. 실제 화면과 작업은 gui 모듈에서 제공한다.

from configurator.gui import run


if __name__ == "__main__":
    raise SystemExit(run())
