import 'dart:async';
import 'dart:convert';

import 'package:app/core/network/api_client.dart';
import 'package:app/core/network/session_store.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;

class MemorySessionStore implements SessionStore {
  final values = <String, String>{};
  final refreshed = Completer<void>();

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
      if (key == 'session' &&
          jsonDecode(value)['access_token'] == 'new-access-token' &&
          !refreshed.isCompleted) {
        // Let ApiClient finish updating its in-memory session first.
        Timer.run(() => refreshed.complete());
      }
    }
  }
}

class RotatingTokenClient extends http.BaseClient {
  RotatingTokenClient(this.refreshed);

  final Completer<void> refreshed;
  final oldRequestWaiting = Completer<void>();
  var oldRequests = 0;
  var newRequests = 0;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    if (request.url.path.endsWith('/auth/refresh')) {
      return _json({
        'access_token': 'new-access-token',
        'refresh_token': 'new-refresh-token',
        'expires_in': 900,
        'user': {'id': 'user-1', 'role': 'viewer'},
      });
    }

    if (request.headers['Authorization'] == 'Bearer old-access-token') {
      oldRequests++;
      if (oldRequests == 2) {
        oldRequestWaiting.complete();
        await refreshed.future;
      }
      return _response(401);
    }

    if (request.headers['Authorization'] == 'Bearer new-access-token') {
      newRequests++;
      return _response(200, body: [1, 2, 3]);
    }
    return _response(401);
  }

  http.StreamedResponse _json(Map<String, Object> value) => http.StreamedResponse(
        Stream.value(utf8.encode(jsonEncode(value))),
        200,
        headers: {'content-type': 'application/json'},
      );

  http.StreamedResponse _response(int status, {List<int> body = const []}) =>
      http.StreamedResponse(Stream.value(body), status);
}

void main() {
  test(
    'retries a concurrent image request with the token another request refreshed',
    () async {
      final store = MemorySessionStore();
      store.values['session'] = jsonEncode({
        'origin': 'https://cctv.example.com',
        'access_token': 'old-access-token',
        'refresh_token': 'old-refresh-token',
        'device_id': 'device-1',
        'user': {'id': 'user-1', 'role': 'viewer'},
        'expires_at': DateTime.now()
            .toUtc()
            .add(const Duration(hours: 1))
            .toIso8601String(),
      });
      final client = RotatingTokenClient(store.refreshed);
      final api = ApiClient(store: store, client: client);
      await api.restore();

      final first = api.getBytes('/api/v1/events/event-1/snapshot');
      final second = api.getBytes('/api/v1/events/event-2/snapshot');
      await client.oldRequestWaiting.future;
      final images = await Future.wait([first, second]);

      expect(images, everyElement(<int>[1, 2, 3]));
      expect(client.oldRequests, 2);
      expect(client.newRequests, 2);
      api.dispose();
    },
  );
}
