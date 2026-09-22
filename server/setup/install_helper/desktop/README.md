# AI CCTV 관리자 데스크톱 화면

설치 도우미에서 실행되는 PyQt 기반 관리자 화면입니다.

- `application.py`: 서버 연결 기반 메인 창
- `settings_window.py`: 관리자 로그인과 모니터링 카메라 선택
- `management.py`: 카메라 등록·수정·화질 변경·게시 계정 전달
- `resource_monitor_window.py`: 서버 및 로컬 리소스 상태 표시

별도 UI 컨테이너를 사용하지 않으며, 화면은 External 서비스의 `/api/v1` API를 호출합니다.
