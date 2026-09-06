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

  bool belongsTo(String? currentUser, String? currentDevice) =>
      userId == currentUser && deviceId == currentDevice;

  bool matchesLocalDate(DateTime date) {
    final local = occurredAt.toLocal();
    return local.year == date.year &&
        local.month == date.month &&
        local.day == date.day;
  }

  Map<String, String> toJson() => {
    'event_id': eventId,
    'user_id': userId,
    'device_id': deviceId,
    'camera_id': cameraId,
    'occurred_at': occurredAt.toIso8601String(),
  };
}
