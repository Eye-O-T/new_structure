// 앱이 화면에 떠 있을 때 받은 FCM도 Android 알림으로 보여주고 탭 정보를 다시 앱에 전달한다.
// 잠금 화면에 사건 상세나 영상이 드러나지 않도록 알림 문구는 일반 안내만 사용한다.
import 'dart:convert';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'notification_payload.dart';

/// OS 알림 표시와 탭 payload 전달을 감싸는 공용 어댑터다.
class LocalNotificationService {
  static final _notifications = FlutterLocalNotificationsPlugin();
  static bool _ready = false;

  /// 탭 콜백과 Android 알림 채널을 준비한 뒤 알림 표시를 허용한다.
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

  /// 종료된 앱을 로컬 알림으로 시작했을 때만 화면 이동에 사용할 payload를 반환한다.
  static Future<String?> launchPayload() async {
    final launch = await _notifications.getNotificationAppLaunchDetails();
    return launch?.didNotificationLaunchApp == true
        ? launch?.notificationResponse?.payload
        : null;
  }

  /// 일반 안내 문구를 표시하고 상세 조회용 식별자는 탭 payload에 담는다.
  static Future<void> showEvent(NotificationPayload event) async {
    if (!_ready) return;
    // 이벤트 ID를 Android가 받는 정수 범위로 줄여 같은 이벤트에 같은 알림 ID를 사용한다.
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

  /// 계정 변경이나 수신 해제 후 이전 알림을 기기 알림함에서 제거한다.
  static Future<void> clear() async {
    if (_ready) await _notifications.cancelAll();
  }
}
