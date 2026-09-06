import 'package:flutter_test/flutter_test.dart';
import 'package:app/main.dart' as app;

// Keep cold startup in its own isolate so native singletons and runApp's
// warm-up frame begin with a fresh application clock.
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
