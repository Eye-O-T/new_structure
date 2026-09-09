// 각 화면이 같은 API 세션을 공유하도록 Riverpod에 등록하고 종료 시 HTTP 연결을 정리한다.
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'api_client.dart';
import 'session_store.dart';

/// ProviderContainer 안에서 보안 저장소와 HTTP 클라이언트의 수명주기를 공유한다.
final apiClientProvider = Provider<ApiClient>((ref) {
  final client = ApiClient(store: SecureSessionStore());
  ref.onDispose(client.dispose);
  return client;
});
