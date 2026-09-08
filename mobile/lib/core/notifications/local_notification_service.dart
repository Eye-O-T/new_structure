// 앱이 화면에 떠 있을 때 받은 FCM도 Android 알림으로 보여주고 탭 정보를 다시 앱에 전달한다.
// 잠금 화면에 사건 상세나 영상이 드러나지 않도록 알림 문구는 일반 안내만 사용한다.
import 'dart:convert';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'notification_payload.dart';

class LocalNotificationService {
  static final _notifications = FlutterLocalNotificationsPlugin();
  static bool _ready = false;

  static Future<void> initialize({
    required void Function(String?) onTap,
  }) async {
    await _notifications.initialize(
      settings: const InitializationSettings(
        android: AndroidInitializationSettings('@mipmap/ic_launcher'),
      ),
      onDidReceiveNotificationResponse: (response) => onTap(response.payload),
    );
    await _notifications
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.createNotificationChannel(
          const AndroidNotificationChannel(
            'cctv_events',
            'CCTV 이벤트',
            description: 'CCTV 이벤트 알림',
            importance: Importance.high,
          ),
        );
    _ready = true;
  }

  static Future<String?> launchPayload() async {
    final launch = await _notifications.getNotificationAppLaunchDetails();
    return launch?.didNotificationLaunchApp == true
        ? launch?.notificationResponse?.payload
        : null;
  }

  static Future<void> showEvent(NotificationPayload event) async {
    if (!_ready) return;
    final id = (int.tryParse(event.eventId) ?? event.eventId.hashCode)
        .remainder(2147483647);
    await _notifications.show(
      id: id,
      title: 'AI CCTV',
      body: '새 CCTV 이벤트가 있습니다. 앱에서 확인하세요.',
      notificationDetails: const NotificationDetails(
        android: AndroidNotificationDetails(
          'cctv_events',
          'CCTV 이벤트',
          channelDescription: 'CCTV 이벤트 알림',
          importance: Importance.high,
          priority: Priority.high,
        ),
      ),
      payload: jsonEncode(event.toJson()),
    );
  }

  static Future<void> clear() async {
    if (_ready) await _notifications.cancelAll();
  }
}
