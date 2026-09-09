# 문서 안내

처음 사용하는 사람은 [프로젝트 소개와 설치 순서](../README.md)를 먼저 읽는다. 아래 영구 문서는 설치·운영·개발 중 계속 참고한다. 구조 그림은 저장소에 포함된 SVG이므로 Mermaid 확장 없이 읽을 수 있다.

| 지금 필요한 일 | 바로가기 |
|---|---|
| 처음 설치하고 카메라·앱 연결 | [프로젝트 안내](../README.md) |
| 서버와 카메라 관리 | [서버 관리자 화면](guide.md#서버-관리자-화면), [호스트 상태 확인](guide.md#호스트-상태와-백업) |
| 백업·복원·업데이트 | [전체 백업](guide.md#전체-백업), [백업 복원](guide.md#백업-복원), [업그레이드](guide.md#업그레이드) |
| 개발 환경·검증 | [소스 배포](guide.md#소스-배포), [개발과 검증](guide.md#개발과-검증) |
| 시스템 흐름·현재 구현 범위 이해 | [전체 구조](architecture.md), [통신·인증·저장소 상세](guide.md#상세-설정-부록) |

## 영구 문서: 설치와 사용

| 문서 | 내용 |
|---|---|
| [설치·운영·개발 상세 안내](guide.md) | Windows 설치 설정, 소스 배포, 모바일 푸시, 백업·복원, 업그레이드, 개발·검증 |
| [Windows 설치 도우미](../server/setup/install_helper/README.md) | 설치 마법사, 기존 서버 관리, Edge 연결, CLI, 설치 파일 빌드 |
| [Raspberry Pi Edge](../edge/README.md) | Pi 패키지 설치, 영상 송출, 장치 연결, 수동 등록, 업데이트 |
| [Android 앱](../mobile/README.md) | 앱 빌드·설정, 로그인과 영상 확인, 푸시 알림 |
| [MP4 모의 카메라](../tests/mock_edge/README.md) | 실제 Pi 없이 카메라 연결과 영상 흐름 시험 |

## 영구 문서: 구조와 인터페이스

| 문서 | 내용 |
|---|---|
| [전체 구조](architecture.md) | SVG 그림으로 보는 구성 요소, 영상·이벤트·복구 흐름과 현재 구현 범위 |
| [공개 API 명세](openapi.yaml) | 모바일 등 외부 클라이언트의 인증·영상·알림 API |

여러 카메라의 동일 인물 연결과 추가 정보(metadata) 분석은 인터페이스만 준비된 미구현 영역이다. 현재 구현 범위는 구조 문서에서, 현행 입출력 계약과 검증 코드는 각 서비스 README에서 확인한다.

## 영구 문서: 서버 구성 요소 개발

| 문서 | 내용 |
|---|---|
| [Data](../server/services/data/README.md) | 데이터·인증·설정 저장, DB 백업 도구와 개발 절차 |
| [External](../server/services/external/README.md) | 공개 API, 서버 관리자 화면, 관리자 생성과 OpenAPI 도구 |
| [Preprocessing](../server/services/preprocessing/README.md) | 영상 감지·추적과 인물 연결 실행 구성 |
| [Analysis](../server/services/analysis/README.md) | 분석 작업 처리와 분석 모듈 실행 구성 |

공통 개발 환경과 자동 테스트 명령은 [개발과 검증](guide.md#개발과-검증)을 따른다. 프로젝트 소스 코드는 [MIT License](../LICENSE)로 제공하며, 제3자 구성요소에는 각자의 라이선스와 배포 조건이 적용된다.

## 임시 교체 인수 문서

다음 두 파일은 다른 담당자가 해당 컨테이너를 구현·교체하고 인수 검증할 때 사용하는 임시 단독 안내다. 일반 설치·운영의 필수 문서가 아니며 Windows 설치 패키지에도 포함하지 않는다. 현재는 저장소에 유지하고, 교체 구현의 인수 완료 후 삭제할 예정이다.

| 파일 | 대상 |
|---|---|
| `docs/SRS_interface_preprocessing.md` | 감지·추적·전역 인물 연결을 맡는 Preprocessing 컨테이너 교체 담당자 |
| `docs/SRS_interface_analysis.md` | 객체의 추가 정보를 분석하는 Analysis 컨테이너 교체 담당자 |

삭제 후에도 영구 문서와 서비스 README의 현행 코드·테스트 안내는 독립적으로 사용할 수 있다.
