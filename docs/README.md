# AI CCTV 문서

- [사용자 안내](user-guide.md): 최초 설치, 카메라 등록, 데스크톱·모바일 사용
- [배포 안내](deployment-guide.md): Windows Server, Edge, Android 준비와 설정
- [운영·복원 안내](operations.md): 상태 점검, 백업, 복구와 장애 대응
- [구성·네트워크](architecture.md): 서비스 책임, 포트와 데이터 흐름
- [API 계약](openapi.yaml): 공개 REST API
- [Server GUI의 Edge 연결 구현 기록](server-edge-gui-interface-plan.md): 신규 연결·등록된 카메라 탭, 처리 순서와 검증 결과
- [Edge–Server 페어링 절차](edge-server-pairing-guide.md): 현재 수동 연결과 GUI 수정 후의 자동 연결 절차
- [Edge–Server 결함 수정 작업 계획](edge-server-remediation-plan.md): 작업 단위별 수정 범위, 선행 조건, 완료 기준과 실장비 인수시험

서비스별 입력·출력과 구현 기준은 각 서비스 폴더의 README에 있습니다. 실제 모델 검증은
해당 README의 인수 확인 절차를 사용하며, 장비별 성능은 대상 장비에서 별도로 기록합니다.
