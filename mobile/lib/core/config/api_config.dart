// API 서버와 영상 주소를 검증한다. 인증 헤더가 다른 서버로 전달되지 않도록
// 영상 URL도 로그인 서버와 같은 HTTPS 호스트·포트로 제한한다.
/// 로그인 서버의 기준 주소와 인증을 전달해도 되는 미디어 주소를 검증한다.
class ApiConfig {
  /// 빌드 시 지정한 최초 로그인 입력값이며 사용자는 로그인 화면에서 바꿀 수 있다.
  static const String initialOrigin = String.fromEnvironment('API_BASE_URL');

  /// HTTPS origin만 허용하고 루트 슬래시를 제거해 저장 형식을 통일한다.
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

  /// 상대 경로를 로그인 서버에 결합하고 동일 호스트·포트인지 확인한다.
  /// 녹화 조회에 쓰는 쿼리 문자열은 유지하되 URL 내부 자격 증명은 거부한다.
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
