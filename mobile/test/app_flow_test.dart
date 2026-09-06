import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:app/main.dart' as app;
import 'package:app/core/network/api_client.dart';
import 'package:app/core/network/providers.dart';
import 'api_client_test.dart' show MemoryStore, json, session;

void main() {
  testWidgets('login to central API, browse history, open event details', (
    tester,
  ) async {
    final event = {
      'id': 42,
      'camera_id': 'cam-001',
      'event_type': 'person_appeared',
      'occurred_at': DateTime.now().toUtc().toIso8601String(),
      'recording_segment_ids': [],
    };
    final api = ApiClient(
      store: MemoryStore(),
      client: MockClient((request) async {
        if (request.url.path.endsWith('/login')) return json(session());
        if (request.url.path.endsWith('/cameras')) {
          return json({
            'items': request.url.queryParameters['offset'] == '0'
                ? [
                    {'camera_id': 'cam-001', 'name': '출입구'},
                  ]
                : [],
          });
        }
        if (request.url.path.endsWith('/events/42')) return json(event);
        if (request.url.path.endsWith('/events')) {
          return json({
            'items': request.url.queryParameters['offset'] == '0'
                ? [event]
                : [],
          });
        }
        return json({}, 404);
      }),
    );
    await api.restore();
    final container = ProviderContainer(
      overrides: [apiClientProvider.overrideWithValue(api)],
    );
    addTearDown(() {
      container.dispose();
      api.dispose();
    });
    await tester.pumpWidget(
      UncontrolledProviderScope(container: container, child: const app.MyApp()),
    );
    await tester.pumpAndSettle();
    expect(find.text('AI CCTV 로그인'), findsOneWidget);
    await tester.enterText(find.byType(TextField).at(0), 'https://cctv.test');
    await tester.enterText(find.byType(TextField).at(1), 'admin');
    await tester.enterText(find.byType(TextField).at(2), 'password');
    await tester.tap(find.widgetWithText(FilledButton, '로그인'));
    await tester.pumpAndSettle();
    expect(find.text('출입구'), findsOneWidget);
    await tester.tap(find.text('히스토리'));
    await tester.pumpAndSettle();
    expect(find.text('사람 등장'), findsOneWidget);
    await tester.tap(find.text('사람 등장'));
    await tester.pumpAndSettle();
    expect(find.text('이벤트 상세'), findsOneWidget);
    expect(find.textContaining('연결된 녹화가 없습니다'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
