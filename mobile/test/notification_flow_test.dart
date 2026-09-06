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
      await api.logout();
      await api.login('https://cctv.test', 'admin', 'password');
      controller.openEvent(data);
      expect(received, [false, true]);
      expect(api.deviceId, isNot(data['device_id']));
    },
  );
}
