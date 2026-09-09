// 실제 네트워크·보안 저장소 없이 인증 회전, 이벤트 계약, URL·알림 검증을 확인한다.
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

/// 플랫폼 플러그인 없이 저장·삭제 결과를 직접 검사할 수 있는 메모리 저장소다.
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

/// 로그인·갱신 응답의 공통 fixture이며 토큰 값을 바꿔 회전 전후를 구분한다.
Map<String, dynamic> session([
  String access = 'old',
  String refresh = 'refresh-old',
]) => {
  'access_token': access,
  'refresh_token': refresh,
  'expires_in': 900,
  'user': {'id': 1, 'username': 'admin', 'role': 'admin', 'is_active': true},
};
/// UTF-8 JSON 응답을 만들어 실제 API 응답 디코딩 경로를 테스트한다.
http.Response json(Object value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);

void main() {
  // 인증 성공 후에는 비밀번호 저장 없이 HTTPS 요청에 Bearer 토큰을 붙여야 한다.
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

  // 두 요청이 같은 만료 토큰으로 실패해도 refresh 토큰 회전은 한 번만 수행해야 한다.
  test('concurrent 401 responses rotate the refresh token only once', () async {
    var refreshCount = 0;
    var oldRequests = 0;
    // 두 요청이 모두 도착할 때까지 401 응답을 보류해 경쟁 상황을 의도적으로 만든다.
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

  // 로그아웃 통신 실패는 재시도 가능하지만 refresh 자격 거부는 로그인 해제로 이어져야 한다.
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

  // 현지 날짜의 UTC 전송, 빈 페이지까지 조회, 알 수 없는 이벤트 종류의 보존을 확인한다.
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

  // 재생에 필요한 쿼리는 유지하면서 다른 origin과 URL 내 자격 증명은 거부해야 한다.
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

  // 자정 근처 이벤트도 사용자의 현지 날짜에 속하고 수신 사용자·기기 검사를 유지해야 한다.
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

  // 구·신규 녹화 필드를 함께 받아도 중복 링크를 만들지 않고 선택적 AI 필드를 허용한다.
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
