// 푸시로 받은 값은 외부 입력이므로 ID·시간 형식을 검증한 뒤 화면 이동에 사용한다.
// 사용자 ID와 기기 ID는 잘못 도착한 알림을 거르는 용도이며 서버의 권한 검사를 대체하지 않는다.
/// 사건 내용 없이 수신 대상 확인과 인증된 상세 조회에 필요한 정보만 보관한다.
class NotificationPayload {
  const NotificationPayload({
    required this.eventId,
    required this.userId,
    required this.deviceId,
    required this.cameraId,
    required this.occurredAt,
  });
  final String eventId;
  final String userId;
  final String deviceId;
  final String cameraId;
  final DateTime occurredAt;

  /// 식별자와 시각이 유효하면 UTC로 정규화하며 잘못된 외부 입력은 null로 거부한다.
  static NotificationPayload? parse(Map<String, dynamic> data) {
    final event = data['event_id']?.toString() ?? '';
    final user = data['user_id']?.toString() ?? '';
    final device = data['device_id']?.toString() ?? '';
    final camera = data['camera_id']?.toString() ?? '';
    final occurred = DateTime.tryParse(data['occurred_at']?.toString() ?? '');
    if (!RegExp(r'^[0-9]+$').hasMatch(event) ||
        !RegExp(r'^[0-9]+$').hasMatch(user) ||
        !RegExp(r'^[a-f0-9]{32}$').hasMatch(device) ||
        occurred == null ||
        !RegExp(r'^[a-z0-9][a-z0-9_-]{0,63}$').hasMatch(camera)) {
      return null;
    }
    return NotificationPayload(
      eventId: event,
      userId: user,
      deviceId: device,
      cameraId: camera,
      occurredAt: occurred.toUtc(),
    );
  }

  /// 같은 사용자라도 이전 로그인에서 발급된 기기 식별자의 알림은 제외한다.
  bool belongsTo(String? currentUser, String? currentDevice) =>
      userId == currentUser && deviceId == currentDevice;

  /// UTC 날짜가 아닌 사용자가 보는 현지 날짜로 히스토리 갱신 여부를 판단한다.
  bool matchesLocalDate(DateTime date) {
    final local = occurredAt.toLocal();
    return local.year == date.year &&
        local.month == date.month &&
        local.day == date.day;
  }

  /// 로컬 알림 탭에서 동일한 검증 경로로 복원할 수 있는 문자열 payload를 만든다.
  Map<String, String> toJson() => {
    'event_id': eventId,
    'user_id': userId,
    'device_id': deviceId,
    'camera_id': cameraId,
    'occurred_at': occurredAt.toIso8601String(),
  };
}
