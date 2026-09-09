// 로그인 토큰을 일반 설정 파일 대신 OS의 보안 저장소에 보관한다.
// 인터페이스를 분리하여 테스트에서는 실제 단말 저장소 없이 동작을 확인할 수 있다.
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// 세션과 알림 설정을 비동기로 읽고 쓰는 저장소 계약이다.
abstract interface class SessionStore {
  /// 저장되지 않은 키는 null을 반환한다.
  Future<String?> read(String key);
  /// null은 문자열 저장이 아닌 키 삭제를 뜻한다.
  Future<void> write(String key, String? value);
}

/// 플랫폼의 보안 저장소를 사용하며 삭제도 같은 인터페이스로 처리한다.
class SecureSessionStore implements SessionStore {
  final FlutterSecureStorage _storage = const FlutterSecureStorage();

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String? value) => value == null
      ? _storage.delete(key: key)
      : _storage.write(key: key, value: value);
}
