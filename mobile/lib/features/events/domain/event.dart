// 이벤트 API 응답을 화면에서 쓰는 자료형으로 변환한다. 시각은 UTC로 보관하고 표시할 때만 바꾼다.
// globalPersonId와 metadata는 서버 작업 결과이며 미설정인 재식별·분석 결과를 앱이 채우지 않는다.
class Event {
  final String id;
  final String cameraId;
  final String eventType;
  final DateTime occurredAt;
  final String? personId;
  final String? globalPersonId;
  final double? confidence;
  final List<String> recordingIds;
  final Map<String, dynamic> metadata;

  const Event({
    required this.id,
    required this.cameraId,
    required this.eventType,
    required this.occurredAt,
    this.personId,
    this.globalPersonId,
    this.confidence,
    this.recordingIds = const [],
    this.metadata = const {},
  });

  factory Event.fromJson(Map<String, dynamic> json) {
    final ids = <String>{
      ...((json['recording_segment_ids'] as List?) ?? []).map(
        (id) => id.toString(),
      ),
      if (json['recording_segment_id'] != null)
        json['recording_segment_id'].toString(),
    };
    return Event(
      id: json['id'].toString(),
      cameraId: json['camera_id'] as String,
      eventType: json['event_type'] as String,
      occurredAt: DateTime.parse(json['occurred_at'] as String).toUtc(),
      personId: json['person_id'] as String?,
      globalPersonId: json['global_person_id'] as String?,
      confidence: (json['confidence'] as num?)?.toDouble(),
      recordingIds: ids.toList(),
      metadata: Map<String, dynamic>.from((json['metadata'] as Map?) ?? {}),
    );
  }

  String get title => eventLabels[eventType] ?? eventType;
}

const eventLabels = {
  'person_detected': '사람 감지',
  'person_appeared': '사람 등장',
  'person_disappeared': '사람 이탈',
  'camera_input_lost': '카메라 입력 중단',
  'camera_input_restored': '카메라 입력 복구',
  'central_connection_lost': '중앙 연결 중단',
  'central_connection_restored': '중앙 연결 복구',
  'inference_stream_lost': 'AI 영상 입력 중단',
  'inference_stream_restored': 'AI 영상 입력 복구',
  'external_power_lost': '외부 전원 중단',
  'external_power_restored': '외부 전원 복구',
  'battery_low': '배터리 부족',
  'battery_critical': '배터리 위험',
  'storage_warning': '저장 공간 부족',
  'storage_critical': '저장 공간 위험',
  'edge_offline': '장치 오프라인',
  'edge_online': '장치 온라인',
  'video_profile_changed': '영상 품질 변경',
  'video_profile_change_failed': '영상 품질 변경 실패',
  'network_failure': '네트워크 장애',
  'network_recovery': '네트워크 복구',
};
