import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:app/core/network/api_client.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/features/events/presentation/event_history_view_model.dart';
import 'api_client_test.dart' show MemoryStore, json, session;

Map<String, Object> event(int id) => {
  'id': id,
  'camera_id': 'cam-001',
  'event_type': 'person_appeared',
  'occurred_at': '2026-09-06T00:00:00Z',
};

void main() {
  test(
    'loads one page, preserves items on error, retries the same cursor and deduplicates',
    () async {
      final cursors = <String?>[];
      var fail = true;
      final api = ApiClient(
        store: MemoryStore(),
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) return json(session());
          if (request.url.path.endsWith('/cameras')) return json({'items': []});
          final cursor = request.url.queryParameters['cursor'];
          cursors.add(cursor);
          if (cursor == null) {
            return json({
              'items': [event(3), event(2)],
              'next_cursor': 'stable-snapshot',
            });
          }
          if (fail) {
            fail = false;
            return json({}, 503);
          }
          return json({
            'items': [event(2), event(1)],
            'next_cursor': null,
          });
        }),
      );
      await api.login('https://cctv.test', 'admin', 'password');
      final container = ProviderContainer(
        overrides: [apiClientProvider.overrideWithValue(api)],
      );
      final subscription = container.listen(eventsProvider, (_, _) {});
      addTearDown(() {
        subscription.close();
        container.dispose();
        api.dispose();
      });
      final first = await container.read(eventsProvider.future);
      expect(first.items.map((e) => e.id), ['3', '2']);
      expect(cursors, [null]);
      await container.read(eventsProvider.notifier).loadMore();
      var state = container.read(eventsProvider).requireValue;
      expect(state.items, hasLength(2));
      expect(state.error, isNotNull);
      await container.read(eventsProvider.notifier).loadMore();
      state = container.read(eventsProvider).requireValue;
      expect(state.items.map((e) => e.id), ['3', '2', '1']);
      expect(state.nextCursor, isNull);
      expect(cursors, [null, 'stable-snapshot', 'stable-snapshot']);
      await container.read(eventsProvider.notifier).loadMore();
      expect(cursors, hasLength(3));
    },
  );

  test('refresh does not reuse the previous cursor while loading', () async {
    final pending = Completer<http.Response>();
    final started = Completer<void>();
    final cursors = <String?>[];
    final api = ApiClient(
      store: MemoryStore(),
      client: MockClient((request) async {
        if (request.url.path.endsWith('/login')) return json(session());
        if (request.url.path.endsWith('/cameras')) return json({'items': []});
        cursors.add(request.url.queryParameters['cursor']);
        if (cursors.length == 1) {
          return json({
            'items': [event(3)],
            'next_cursor': 'previous-snapshot',
          });
        }
        if (cursors.length == 2) {
          started.complete();
          return pending.future;
        }
        return json({'items': []});
      }),
    );
    await api.login('https://cctv.test', 'admin', 'password');
    final container = ProviderContainer(
      overrides: [apiClientProvider.overrideWithValue(api)],
    );
    final subscription = container.listen(eventsProvider, (_, _) {});
    addTearDown(() {
      subscription.close();
      container.dispose();
      api.dispose();
    });
    await container.read(eventsProvider.future);
    container.invalidate(eventsProvider);
    final refreshed = container.read(eventsProvider.future);
    await started.future;
    await container.read(eventsProvider.notifier).loadMore();
    expect(cursors, [null, null]);
    pending.complete(
      json({
        'items': [event(9)],
        'next_cursor': null,
      }),
    );
    expect((await refreshed).items.single.id, '9');
  });

  test(
    'changing date discards an in-flight older page and resets the snapshot',
    () async {
      final pending = Completer<http.Response>();
      final started = Completer<void>();
      final cursors = <String?>[];
      var requests = 0;
      final api = ApiClient(
        store: MemoryStore(),
        client: MockClient((request) async {
          if (request.url.path.endsWith('/login')) return json(session());
          if (request.url.path.endsWith('/cameras')) return json({'items': []});
          cursors.add(request.url.queryParameters['cursor']);
          requests++;
          if (requests == 1) {
            return json({
              'items': [event(3)],
              'next_cursor': 'old-snapshot',
            });
          }
          if (requests == 2) {
            started.complete();
            return pending.future;
          }
          return json({
            'items': [event(9)],
            'next_cursor': null,
          });
        }),
      );
      await api.login('https://cctv.test', 'admin', 'password');
      final container = ProviderContainer(
        overrides: [apiClientProvider.overrideWithValue(api)],
      );
      final subscription = container.listen(eventsProvider, (_, _) {});
      addTearDown(() {
        subscription.close();
        container.dispose();
        api.dispose();
      });
      await container.read(eventsProvider.future);
      final more = container.read(eventsProvider.notifier).loadMore();
      await started.future;
      container
          .read(selectedDateProvider.notifier)
          .selectDate(DateTime(2025, 1, 2));
      final selected = await container.read(eventsProvider.future);
      expect(selected.items.single.id, '9');
      pending.complete(
        json({
          'items': [event(1)],
          'next_cursor': 'stale',
        }),
      );
      await more;
      expect(container.read(eventsProvider).requireValue.items.single.id, '9');
      expect(cursors, [null, 'old-snapshot', null]);
    },
  );
}
