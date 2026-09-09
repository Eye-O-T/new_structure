// 좌표 배율·영상 비율 변경·오래된 응답 처리와 화면 해제 후 폴링 중단을 확인한다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:app/core/network/api_client.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/features/live/presentation/object_overlay.dart';
import 'api_client_test.dart' show MemoryStore, json, session;

/// 100×100 원본의 알려진 박스를 사용해 화면으로 변환된 위치를 정확히 검증한다.
Map<String, dynamic> frame() => {
  'camera_id': 'cam-001',
  'frame_width': 100,
  'frame_height': 100,
  'stale': false,
  'objects': [
    {
      'person_id': '7',
      'global_person_id': 'global-1',
      'bbox': [10, 20, 90, 100],
      'confidence': .8,
    },
  ],
};

/// 원본 프레임의 두 배 크기로 제한한 영상 영역을 테스트마다 동일하게 제공한다.
Widget viewport(Widget child) => MaterialApp(
  home: Center(child: SizedBox(width: 200, height: 200, child: child)),
);

void main() {
  testWidgets('boxes scale to video area and label both identities', (
    tester,
  ) async {
    await tester.pumpWidget(viewport(ObjectBoxes(frame: frame())));
    expect(find.text('P:7 G:global-1'), findsOneWidget);
    final positioned = tester.widget<Positioned>(find.byType(Positioned));
    expect(positioned.left, 20);
    expect(positioned.top, 40);
    expect(positioned.width, 160);
    expect(positioned.height, 160);
  });

  testWidgets(
    'a different video aspect ratio hides stale profile coordinates',
    (tester) async {
      await tester.pumpWidget(
        viewport(ObjectBoxes(frame: {...frame(), 'frame_width': 200})),
      );
      expect(find.text('P:7 G:global-1'), findsNothing);
    },
  );

  testWidgets('polling hides stale detections and cancels on disposal', (
    tester,
  ) async {
    bool stale = false;
    int requests = 0;
    final api = ApiClient(
      store: MemoryStore(),
      client: MockClient((request) async {
        if (request.url.path.endsWith('/login')) return json(session());
        requests++;
        expect(request.url.path, '/api/v1/cameras/cam-001/objects');
        expect(request.headers['Authorization'], 'Bearer old');
        return json(stale ? {'objects': [], 'stale': true} : frame());
      }),
    );
    await api.login('https://cctv.test', 'admin', 'password');
    await tester.pumpWidget(
      ProviderScope(
        overrides: [apiClientProvider.overrideWithValue(api)],
        child: viewport(const ObjectOverlay(cameraId: 'cam-001')),
      ),
    );
    await tester.pump();
    await tester.pump();
    expect(find.text('P:7 G:global-1'), findsOneWidget);
    // 다음 폴링 응답을 stale로 바꿔 이전에 표시한 박스도 제거되는지 확인한다.
    stale = true;
    await tester.pump(const Duration(milliseconds: 500));
    await tester.pump();
    expect(find.text('P:7 G:global-1'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    // 위젯 해제 후 가상 시간을 더 진행해 취소되지 않은 타이머 요청이 없는지 확인한다.
    final count = requests;
    await tester.pump(const Duration(seconds: 2));
    expect(requests, count);
    api.dispose();
  });
}
