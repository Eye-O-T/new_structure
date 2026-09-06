import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'notification_controller.dart';

final notificationControllerProvider = Provider<NotificationController>((ref) {
  throw StateError(
    'Notification controller must be provided by the application',
  );
});
