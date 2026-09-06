import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:app/core/config/api_config.dart';
import 'package:app/core/network/api_client.dart';
import 'package:app/core/network/session_store.dart';
import 'package:app/core/notifications/notification_payload.dart';
import 'package:app/features/events/data/api_event_repository.dart';
import 'package:app/features/events/domain/event.dart';

class MemoryStore implements SessionStore {
  final values = <String, String>{};
  @override
  Future<String?> read(String key) async => values[key];
  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }
}

Map<String, dynamic> session([
  String access = 'old',
  String refresh = 'refresh-old',
]) => {
  'access_token': access,
  'refresh_token': refresh,
  'expires_in': 900,
  'user': {'id': 1, 'username': 'admin', 'role': 'admin', 'is_active': true},
};
http.Response json(Object value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);

void main() {
  test(
    'login stores session without password and requests use HTTPS Bearer',
    () async {
      final store = MemoryStore();
      final api = ApiClient(
        store: store,
        client: MockClient((request) async {
          expect(request.url.origin, 'https://cctv.test');
          expect(request.followRedirects, isFalse);
          if (request.url.path.endsWith('/login')) return json(session());
          expect(request.headers['Authorization'], 'Bearer old');
          return json({'items': []});
        }),
      );
      addTearDown(api.dispose);
      await api.login(
        'https://cctv.test',
        'admin',
        'never-store-this-password',
      );
      await api.request('GET', '/api/v1/cameras');
      expect(
        store.values['session'],
        isNot(contains('never-store-this-password')),
      );
      expect(api.isAdmin, isTrue);
    },
  );

  test('concurrent 401 responses rotate the refresh token only once', () async {
    var refreshCount = 0;
    var oldRequests = 0;
    final bothRequests = Completer<void>();
    final api = ApiClient(
      store: MemoryStore(),
      client: MockClient((request) async {
        if (request.url.path.endsWith('/login')) return json(session());
        if (request.url.path.endsWith('/refresh')) {
          refreshCount++;
          expect(jsonDecode(request.body)['refresh_token'], 'refresh-old');
          await Future<void>.delayed(const Duration(milliseconds: 20));
          return json(session('new', 'refresh-new'));
        }
        if (request.headers['Authorization'] == 'Bearer old') {
          oldRequests++;
          if (oldRequests == 2) bothRequests.complete();
          await bothRequests.future;
          return json({}, 401);
        }
        expect(request.headers['Authorization'], 'Bearer new');
        return json({'items': []});
      }),
    );
    addTearDown(api.dispose);
    await api.login('https://cctv.test', 'admin', 'password');
    await Future.wait([
      api.request('GET', '/api/v1/events'),
      api.request('GET', '/api/v1/cameras'),
    ]);
    expect(refreshCount, 1);
    expect(api.refreshToken, 'refresh-new');
  });

  test(
    'failed refresh clears session; failed logout preserves it for retry',
    () async {
      var failRefresh = false;
      var failLogout = true;
      final api = ApiClient(
        store: MemoryStore(),
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) return json(session());
          if (request.url.path.endsWith('/logout')) {
            return json({}, failLogout ? 503 : 204);
          }
          if (request.url.path.endsWith('/refresh')) {
            return json({}, failRefresh ? 401 : 200);
          }
          return json({}, 401);
        }),
      );
      addTearDown(api.dispose);
      await api.login('https://cctv.test', 'admin', 'password');
      await expectLater(api.logout(), throwsA(isA<ApiException>()));
      expect(api.signedIn, isTrue);
      failLogout = false;
      await api.logout();
      expect(api.signedIn, isFalse);
      await api.login('https://cctv.test', 'admin', 'password');
      failRefresh = true;
      await expectLater(
        api.request('GET', '/api/v1/events'),
        throwsA(isA<ApiException>()),
      );
      expect(api.signedIn, isFalse);
    },
  );

  test('event contract paginates to empty and sends UTC bounds', () async {
    final offsets = <String>[];
    final api = ApiClient(
      store: MemoryStore(),
      client: MockClient((request) async {
        if (request.url.path.endsWith('/login')) return json(session());
        expect(request.url.path, '/api/v1/events');
        expect(request.url.queryParameters['camera_id'], 'cam-001');
        final from = request.url.queryParameters['from']!;
        expect(from, endsWith('Z'));
        expect(DateTime.parse(from).toLocal(), DateTime(2026, 9, 6));
        final offset = request.url.queryParameters['offset']!;
        offsets.add(offset);
        return json({
          'items': offset == '0'
              ? [
                  {
                    'id': 1,
                    'camera_id': 'cam-001',
                    'event_type': 'new_future_event',
                    'occurred_at': '2026-09-06T00:00:00Z',
                    'metadata': {'custom': 42},
                  },
                ]
              : [],
        });
      }),
    );
    addTearDown(api.dispose);
    await api.login('https://cctv.test', 'admin', 'password');
    final events = await ApiEventRepository(
      apiClient: api,
      cameraId: 'cam-001',
    ).getEventsByDate(DateTime(2026, 9, 6));
    expect(offsets, ['0', '1']);
    expect(events.single.title, 'new_future_event');
    expect(events.single.metadata['custom'], 42);
  });

  test('media URLs preserve query and reject cross-origin credentials', () {
    final origin = ApiConfig.parseOrigin('https://cctv.test');
    expect(
      ApiConfig.mediaUri(
        origin,
        '/playback/get?path=cam-001&duration=60',
      ).query,
      'path=cam-001&duration=60',
    );
    for (final url in [
      'http://cctv.test/x',
      'https://evil.test/x',
      '//evil.test/x',
      'https://user:pass@cctv.test/x',
    ]) {
      expect(() => ApiConfig.mediaUri(origin, url), throwsFormatException);
    }
    expect(
      () => ApiConfig.parseOrigin('http://10.0.2.2:8000'),
      throwsFormatException,
    );
  });

  test('notification identity and local day survive UTC midnight boundary', () {
    final localTime = DateTime(2026, 9, 6, 0, 30);
    final payload = NotificationPayload.parse({
      'event_id': '12',
      'user_id': '1',
      'device_id': 'a' * 32,
      'camera_id': 'cam-001',
      'occurred_at': localTime.toUtc().toIso8601String(),
    })!;
    expect(payload.matchesLocalDate(DateTime(2026, 9, 6)), isTrue);
    expect(payload.belongsTo('1', 'a' * 32), isTrue);
    expect(payload.belongsTo('2', 'a' * 32), isFalse);
    expect(
      NotificationPayload.parse({...payload.toJson(), 'event_id': '../admin'}),
      isNull,
    );
  });

  test(
    'recording links are deduplicated and optional AI fields are not required',
    () {
      final event = Event.fromJson({
        'id': 7,
        'camera_id': 'cam-001',
        'event_type': 'edge_online',
        'occurred_at': '2026-09-06T00:00:00Z',
        'recording_segment_id': 2,
        'recording_segment_ids': [2, 3],
      });
      expect(event.recordingIds, ['2', '3']);
      expect(event.title, '장치 온라인');
      expect(event.personId, isNull);
      expect(event.globalPersonId, isNull);
      final personEvent = Event.fromJson({
        'id': 8,
        'camera_id': 'cam-002',
        'event_type': 'person_appeared',
        'occurred_at': '2026-09-06T00:00:00Z',
        'person_id': '7',
        'global_person_id': 'global-2',
      });
      expect(personEvent.personId, '7');
      expect(personEvent.globalPersonId, 'global-2');
    },
  );
}
