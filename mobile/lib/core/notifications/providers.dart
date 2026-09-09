// main.dart가 라우터와 연결해 만든 알림 관리자를 주입한다. 중복 생성하면 알림을 중복 구독한다.
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'notification_controller.dart';

/// 앱 시작점의 override가 필수이며 누락 시 중복 관리자 생성 대신 명시적으로 실패한다.
final notificationControllerProvider = Provider<NotificationController>((ref) {
  throw StateError(
    'Notification controller must be provided by the application',
  );
});
