// Firebase 연결 설정이 없는 환경에서도 앱 초기화가 로그인 화면까지 진행되는지 확인한다.
import 'package:flutter_test/flutter_test.dart';
import 'package:app/main.dart' as app;

// 네이티브 싱글턴과 첫 프레임의 시계를 분리하기 위해 시작 테스트를 별도 isolate에서 실행한다.
void main() {
  testWidgets('fresh application boots without Firebase configuration', (
    tester,
  ) async {
    app.main();
    await tester.pump();
    await tester.pumpAndSettle();
    expect(find.text('AI CCTV 로그인'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
