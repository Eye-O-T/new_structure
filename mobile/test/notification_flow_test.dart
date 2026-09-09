// 푸시 서비스 연결 없이 전경 알림 중복 제거와 로그인 세션별 탭 허용 범위를 검증한다.
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:app/core/network/api_client.dart';
import 'package:app/core/notifications/notification_controller.dart';
import 'api_client_test.dart' show MemoryStore, json, session;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test(
    'foreground deduplicates and tap opens only the current login session event',
    () async {
      final api = ApiClient(
        store: MemoryStore(),
        client: MockClient(
          (request) async => request.url.path.endsWith('/logout')
              ? json({}, 204)
              : json(session()),
        ),
      );
      await api.login('https://cctv.test', 'admin', 'password');
      // false는 조회 갱신, true는 상세 화면 이동이므로 한 이벤트의 두 의도를 구분한다.
      final received = <bool>[];
      final controller = NotificationController(
        api,
        onResume: () {},
        onEvent: (payload, open) => received.add(open),
      );
      addTearDown(() {
        controller.dispose();
        api.dispose();
      });
      final data = <String, dynamic>{
        'event_id': '42',
        'user_id': '1',
        'device_id': api.deviceId,
        'camera_id': 'cam-001',
        'occurred_at': DateTime.now().toUtc().toIso8601String(),
      };
      await controller.handleForeground(data);
      await controller.handleForeground(data);
      controller.openEvent(data);
      controller.openEvent({...data, 'user_id': '2'});
      expect(received, [false, true]);
      // 같은 계정으로 재로그인해도 이전 로그인 기기 ID의 알림은 다시 열리지 않아야 한다.
      await api.logout();
      await api.login('https://cctv.test', 'admin', 'password');
      controller.openEvent(data);
      expect(received, [false, true]);
      expect(api.deviceId, isNot(data['device_id']));
    },
  );
}
