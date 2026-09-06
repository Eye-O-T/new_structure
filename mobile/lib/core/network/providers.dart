import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'api_client.dart';
import 'session_store.dart';

final apiClientProvider = Provider<ApiClient>((ref) {
  final client = ApiClient(store: SecureSessionStore());
  ref.onDispose(client.dispose);
  return client;
});
