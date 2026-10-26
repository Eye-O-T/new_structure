# 운영·백업·복원 안내

이 절차는 Windows Server와 Pi Edge가 신뢰 LAN에 있는 시연 환경을 기준으로 합니다.
실제 사용자 자료를 다루기 전에 저장 위치와 남은 디스크 용량을 확인하십시오.

## 상태 확인

Windows에서 Server Manager의 상태·진단·로그 화면을 사용합니다. CLI에서는 저장소의
`ai_cctv.ps1 cli status`, `doctor`, `logs`를 사용합니다. `ready` 상태와 실제 카메라 영상,
이벤트 발생, DB·저장소 경로를 각각 확인해야 합니다. HTTP health 응답 하나만으로 영상
처리나 푸시 성공을 판단하지 않습니다.

Pi에서는 다음을 사용합니다.

```bash
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
sudo ai-cctv-edge logs
```

Pi 주소가 바뀌면 Server Manager에서 등록 주소를 갱신합니다. 장애 중 Edge의 로컬 녹화는
중앙 복구 작업자가 다시 연결된 뒤 manifest와 파일 크기·SHA-256을 확인해 가져옵니다.
복구 진행 상태와 마지막 오류는 Server 진단 화면 또는 Data 준비 상태에서 확인합니다.

## 운영과 백업

### 데이터베이스 백업

SQLite 파일을 실행 중에 복사하지 말고 Data 서비스가 제공하는 백업 API를 사용합니다.
저장소 소스에서는 다음과 같이 실행합니다.

```powershell
python server/services/data/tools/backup_database.py
```

설치 환경에서는 설치된 `server` 디렉터리와 ProgramData 환경 파일을 지정합니다.

```powershell
python server/services/data/tools/backup_database.py `
  --server-dir 'C:\Program Files\AI CCTV Server\server' `
  --env-file 'C:\ProgramData\AI_CCTV\config\compose.env'
```

도구는 Docker Compose의 Data 컨테이너에 요청해 일관된 SQLite 복사본을 `DATABASE_DIR`의
`backups/` 아래에 만듭니다. 파일명은 단순 `.db` 이름만 허용하며 기존 파일을 덮어쓰지
않습니다. 이 DB 백업에는 녹화 영상, 스냅샷, 모델, 설정, TLS 인증서·키가 포함되지 않으므로
해당 파일과 폴더는 별도로 복사하고 안전하게 보관하십시오.

## 복원과 보관

현재 백업 도구는 **DB 백업 생성만 지원하며 자동 복원 명령은 제공하지 않습니다.** 복원은
Data를 포함한 Compose 서비스를 정상 종료하고, 현재 DB와 관련 `-wal`·`-shm` 파일을 별도
보존한 다음 진행해야 합니다. 백업 파일을 운영 DB 경로에 적용하기 전에 실제 설정의
`DATABASE_DIR`, DB 파일명, 파일 소유권을 확인하고 백업 복사본에서 SQLite 무결성을
검증하십시오. 복원 후 마이그레이션·로그인·카메라·녹화 인덱스를 확인하기 전까지 원본 DB와
백업을 삭제하지 마십시오. 파일 교체에 익숙하지 않으면 운영자에게 도움을 요청하십시오.

보관 작업은 오래된 녹화와 이벤트·이미지를 정리합니다. Preprocessing 미전송 이벤트의
보호 목록이 없거나 오래되면 이벤트·이미지 정리는 안전을 위해 보류될 수 있습니다. AI를
다시 켠 뒤 보호 목록이 갱신되고 정리가 재개되는지 확인하십시오. 보류 이유와 기간을
진단에 기록해 복구 시점을 추적합니다. 정리 보류 상태가 지속되더라도 보호 파일을 수동
삭제하지 마십시오.

## 장애 복구 점검

네트워크 단절 후 Pi가 중앙으로 다시 송출하는지, 중앙 녹화가 이어지는지, 끊긴 시간대의
Edge 녹화가 복구되는지 확인합니다. Windows 재시작 후 Data·External·MediaMTX·Nginx가
준비되고, 등록된 카메라 영상 및 이벤트 조회가 복원되는지도 시연 전에 확인하십시오.
푸시를 사용하는 경우 별도로 Android 권한, 기기 등록, 실제 수신을 점검합니다.
