# 인물 식별 margin 0 적용 실행 기록

- 적용일: 2026-11-16 21:03 KST
- 대상: 현재 설치 환경의 Data 서비스
- 결과: 설정 적용과 기본 정상성 검증 통과

## 적용 내용

- `C:\ProgramData\AI_CCTV\config\compose.env`에서 `IDENTITY_MATCH_MARGIN`을 `0.05`에서 `0`으로 변경했다.
- `IDENTITY_MATCH_THRESHOLD`는 `0.6`으로 유지했다.
- 변경 전 설정 백업: `C:\ProgramData\AI_CCTV\config\compose.env.bak-20261116-210303` (1,554 bytes).
- Compose 검사와 계획된 Compose 구성 확인에 사용된 파일: `server/compose.yml`, `server/compose.gpu.yml`.
- Data만 재생성했다. Compose 프로젝트는 `ai-cctv`, Data 이미지 태그는 `ai-cctv-data:0.3.0`이다.
- 재생성 시각: `2026-11-16T12:03:05.683Z` (21:03:05 KST).
- 실행 이미지 ID와 `ai-cctv-data:0.3.0` 태그 이미지 ID가 모두 `sha256:6b5528c58630d2908c420e7d4ac1cdb92f85bb73ff42650eccf80481073ef734`로 일치한다.

## 검증 결과

1. 운영 env 값: `IDENTITY_MATCH_MARGIN=0`, `IDENTITY_MATCH_THRESHOLD=0.6`.
2. Data 컨테이너 환경 값: margin `0`, threshold `0.6`.
3. 전처리 컨테이너의 기존 CUDA OSNet으로 이벤트 215·218·221 원본 crop을 재추론했다. 점수는 gpid 2 후보 `0.601942873930674`, gpid 1 후보 `0.5696403351993586`으로 기존 결과와 일치했다.
4. Data 저장소의 실제 `resolve_identity` 코드와 원본 crop 특징을 사용하고 임시 메모리 SQLite만 만들어 분리 재현했다.

| 임시 검증 조건 | 결과 |
|---|---|
| margin 0.05, 같은 1·2위 후보 | gpid 3, `new` |
| margin 0, 같은 1·2위 후보 | gpid 2, `matched` |
| 최고 점수 0.599999, margin 0 | `new` |
| 최고 점수 0.6, margin 0 동점 | `matched` |

5. Data 내부 `/health/ready`: HTTP 200, `ready`; DB `foreign_keys=true`, `journal_mode=wal`.
6. 확인 시점 Data 컨테이너: `healthy`. 다른 AI 및 영상 컨테이너도 `healthy`로 표시됐다.
7. 확인 시점 identity 큐: complete 6, pending 0, running 0, failed 0. Analysis 큐도 complete 6, pending·running·failed 0.
8. DB 이벤트 수 234, 최신 이벤트 ID 234, 최신 관측 `2026-11-16T11:43:01.353Z`로 재생성 전 기록이었다.

## 남은 확인

margin 0이 포함된 새 identity 결과는 아직 발생하지 않았다. 새 사람 track이 들어왔을 때 `metadata.identity.result.match.margin=0.0`과 판정 결과를 확인하면 운영 입력 검증까지 완료할 수 있다. 기존 gpid 2와 3은 이 설정 변경만으로 병합되지 않는다.
