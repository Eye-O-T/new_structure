// 로그인 토큰을 일반 설정 파일 대신 OS의 보안 저장소에 보관한다.
// 인터페이스를 분리하여 테스트에서는 실제 단말 저장소 없이 동작을 확인할 수 있다.
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

abstract interface class SessionStore {
  Future<String?> read(String key);
  Future<void> write(String key, String? value);
}

class SecureSessionStore implements SessionStore {
  final FlutterSecureStorage _storage = const FlutterSecureStorage();

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String? value) => value == null
      ? _storage.delete(key: key)
      : _storage.write(key: key, value: value);
}
