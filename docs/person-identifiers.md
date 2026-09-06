# 사람 식별자

- `person_id`: 카메라 내 추적 ID. 같은 카메라의 추적 실행 구간에서 해석한다. 다른 카메라나 재시작 이후 같은 번호가 나와도 동일 인물이라는 의미는 아니다.
- `global_person_id`: 여러 카메라에서 같은 인물로 연결한 ID. 카메라 간 인물 연결 기능은 아직 없으며 현행 추론기는 `null`을 보낸다. 실명이나 사용자 계정 ID와 무관하다.
- 이벤트에는 두 필드를 독립적으로 저장하고 공개 API와 모바일 이벤트 상세에 전달한다. 사람과 무관한 이벤트에는 둘 다 없어도 된다.

기존 `track_id` 필드는 현행 추론·API에서 제거했다. 이전 추론기나 API 클라이언트가 있다면 함께 갱신해야 한다. Data와 External, Preprocessing를 같은 버전으로 배포한다.

Data 시작 시 `005_person_identifiers.sql`이 기존 DB를 이관한다. 기존 `track_id`가 있으면 `person_id`로 옮기고, 없으면 기존 `person_id`를 유지한다. 두 값이 달랐던 행의 기존 `person_id`는 `metadata.legacy_person_id`에 보존한다. 기존 데이터로 전역 동일 인물을 추정하지 않으며 `global_person_id`는 `null`로 시작한다. 과거 마이그레이션 파일은 변경하지 않는다.

객체 처리 분리 이후의 `tracking_session_id`, 전역 ID 반영과 담당 개발자 인터페이스는 [객체 처리 설계](object-processing.md)를 따른다.
