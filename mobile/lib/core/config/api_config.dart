// API 서버와 영상 주소를 검증한다. 인증 헤더가 다른 서버로 전달되지 않도록
// 영상 URL도 로그인 서버와 같은 HTTPS 호스트·포트로 제한한다.
class ApiConfig {
  static const String initialOrigin = String.fromEnvironment('API_BASE_URL');

  static Uri parseOrigin(String value) {
    final uri = Uri.parse(value.trim());
    if (uri.scheme != 'https' ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw const FormatException('서버 주소는 https://도메인 형식으로 입력하세요.');
    }
    return uri.replace(path: '');
  }

  static Uri mediaUri(Uri origin, String value) {
    final uri = origin.resolve(value);
    if (uri.scheme != 'https' ||
        uri.host != origin.host ||
        uri.port != origin.port ||
        uri.userInfo.isNotEmpty ||
        uri.hasFragment) {
      throw const FormatException('서버와 같은 HTTPS 주소의 영상만 재생할 수 있습니다.');
    }
    return uri;
  }
}
