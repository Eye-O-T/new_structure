import 'dart:async';
import 'dart:convert';

import 'package:app/core/network/api_client.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'api_client_test.dart' show MemoryStore, json, session;

class _DelayedStore extends MemoryStore {
  final started = Completer<void>();
  final release = Completer<void>();
  int activeWrites = 0;
  int maxActiveWrites = 0;
  bool failNext = false;

  @override
  Future<void> write(String key, String? value) async {
    activeWrites++;
    if (activeWrites > maxActiveWrites) maxActiveWrites = activeWrites;
    try {
      if (value?.contains('renewed-access') == true) {
        started.complete();
        await release.future;
      }
      if (failNext) {
        failNext = false;
        throw StateError('secure storage unavailable');
      }
      await super.write(key, value);
    } finally {
      activeWrites--;
    }
  }
}

void main() {
  test(
    'a pending new login write cannot send the previous token to its origin',
    () async {
      final store = _DelayedStore();
      final requests = <http.Request>[];
      final api = ApiClient(
        store: store,
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) {
            return json(
              session(
                request.url.host == 'first.test'
                    ? 'first-access'
                    : 'renewed-access',
              ),
            );
          }
          requests.add(request);
          return json({'items': []});
        }),
      );
      addTearDown(api.dispose);
      await api.login('https://first.test', 'account-a', 'password');
      final login = api.login('https://second.test', 'account-b', 'password');
      await store.started.future;
      await api.request('GET', '/api/v1/cameras');
      expect(requests.single.url.origin, 'https://first.test');
      expect(requests.single.headers['Authorization'], 'Bearer first-access');
      store.release.complete();
      await login;
      await api.request('GET', '/api/v1/cameras');
      expect(requests.last.url.origin, 'https://second.test');
      expect(requests.last.headers['Authorization'], 'Bearer renewed-access');
    },
  );

  test(
    'a delayed refresh write finishes before another origin can log in',
    () async {
      final store = _DelayedStore();
      final requests = <http.Request>[];
      final api = ApiClient(
        store: store,
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) {
            return json(session('${request.url.host}-access'));
          }
          if (request.url.path.endsWith('/refresh')) {
            return json(session('renewed-access', 'renewed-refresh'));
          }
          requests.add(request);
          return json({'items': []});
        }),
      );
      addTearDown(api.dispose);
      await api.login('https://first.test', 'account-a', 'password');
      final refresh = api.refreshSession();
      await store.started.future;
      final login = api.login('https://second.test', 'account-b', 'password');
      await api.request('GET', '/api/v1/cameras');
      expect(requests.single.url.origin, 'https://first.test');
      expect(
        requests.single.headers['Authorization'],
        'Bearer first.test-access',
      );
      store.release.complete();
      await Future.wait([refresh, login]);
      await api.request('GET', '/api/v1/cameras');
      expect(requests.last.url.origin, 'https://second.test');
      expect(
        requests.last.headers['Authorization'],
        'Bearer second.test-access',
      );
      expect(
        jsonDecode(store.values['session']!)['origin'],
        'https://second.test',
      );
      expect(
        jsonDecode(store.values['session']!)['access_token'],
        'second.test-access',
      );
      expect(store.maxActiveWrites, 1);
    },
  );

  test(
    'logout waits for an earlier refresh write and removes its persisted session',
    () async {
      final store = _DelayedStore();
      String? revokedRefresh;
      final api = ApiClient(
        store: store,
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) return json(session());
          if (request.url.path.endsWith('/refresh')) {
            return json(session('renewed-access', 'renewed-refresh'));
          }
          revokedRefresh = jsonDecode(request.body)['refresh_token'] as String;
          return json({}, 204);
        }),
      );
      addTearDown(api.dispose);
      await api.login('https://first.test', 'account-a', 'password');
      final refresh = api.refreshSession();
      await store.started.future;
      final logout = api.logout();
      store.release.complete();
      await Future.wait([refresh, logout]);
      expect(revokedRefresh, 'renewed-refresh');
      expect(api.signedIn, isFalse);
      expect(store.values['session'], isNull);
      expect(store.maxActiveWrites, 1);
    },
  );

  test(
    'a refresh queued during logout cannot revive the old session',
    () async {
      final logoutStarted = Completer<void>();
      final logoutResponse = Completer<http.Response>();
      var refreshRequests = 0;
      final store = MemoryStore();
      final api = ApiClient(
        store: store,
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) {
            return json(session('${request.url.host}-access'));
          }
          if (request.url.path.endsWith('/logout')) {
            logoutStarted.complete();
            return logoutResponse.future;
          }
          if (request.url.path.endsWith('/refresh')) {
            refreshRequests++;
            return json(session('renewed-access'));
          }
          expect(request.url.origin, 'https://second.test');
          expect(request.headers['Authorization'], 'Bearer second.test-access');
          return json({'items': []});
        }),
      );
      addTearDown(api.dispose);
      await api.login('https://first.test', 'account-a', 'password');
      final logout = api.logout();
      await logoutStarted.future;
      final refresh = expectLater(
        api.refreshSession(),
        throwsA(isA<ApiException>()),
      );
      final login = api.login('https://second.test', 'account-b', 'password');
      logoutResponse.complete(json({}, 204));
      await Future.wait([logout, refresh, login]);
      expect(refreshRequests, 0);
      await api.request('GET', '/api/v1/cameras');
      expect(
        jsonDecode(store.values['session']!)['origin'],
        'https://second.test',
      );
    },
  );

  test(
    'a failed login write preserves the whole previous session and leaves the queue usable',
    () async {
      final store = _DelayedStore();
      final api = ApiClient(
        store: store,
        client: MockClient(
          (request) async => json(session('${request.url.host}-access')),
        ),
      );
      addTearDown(api.dispose);
      await api.login('https://first.test', 'account-a', 'password');
      final persisted = store.values['session'];
      store.failNext = true;
      await expectLater(
        api.login('https://second.test', 'account-b', 'password'),
        throwsStateError,
      );
      expect(api.origin?.origin, 'https://first.test');
      expect(await api.accessToken(), 'first.test-access');
      expect(store.values['session'], persisted);
      await api.login('https://second.test', 'account-b', 'password');
      expect(api.origin?.origin, 'https://second.test');
      expect(await api.accessToken(), 'second.test-access');
    },
  );
}
