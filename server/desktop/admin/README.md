# AI CCTV 관제 화면

서버 설치 도우미·서버 제어판과 독립적으로 실행되는 PyQt 기반 CCTV 관제 화면입니다.

- `application.py`: 서버 연결 기반 메인 창, 이벤트 타임라인(`analysis_completed` 분석 완료 카드·지연 `gpid` 갱신) 및 상세 창(`analysis_completed` 전용 분석 결과 표시, `person_appeared` 미디어·녹화 표시 분리)
- `video_worker.py`: 실시간 영상 수신, 객체 좌표 및 증분 이벤트 수집, 미확정 `gpid` 완료 이벤트의 5초 주기 보완 조회
- `settings_window.py`: 관리자 로그인과 모니터링 카메라 선택
- `management.py`: 카메라 등록·수정·화질 변경·게시 계정 전달
- `resource_monitor_window.py`: 서버 및 로컬 리소스 상태 표시

별도 UI 컨테이너를 사용하지 않으며, 화면은 External 서비스의 `/api/v1` API를 호출합니다.
