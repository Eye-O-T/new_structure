// 모든 JSON API 요청의 로그인 토큰, 만료 갱신, 오류 변환을 한곳에서 처리한다.
// 영상 데이터 자체의 재생은 ProtectedVideo가 맡으며 이 클래스는 주소와 인증을 제공한다.
import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../config/api_config.dart';
import 'session_store.dart';

/// 화면에 표시할 요청 실패 정보이며 statusCode 0은 HTTP 응답 외의 실패를 뜻한다.
class ApiException implements Exception {
  const ApiException(this.statusCode, this.message);
  final int statusCode;
  final String message;
  @override
  String toString() => message;
}

/// 인증 세션을 소유하고 로그인 상태 변경을 라우터와 알림 관리자에게 알린다.
class ApiClient extends ChangeNotifier {
  ApiClient({required this.store, http.Client? client})
    : _client = client ?? http.Client();

  final SessionStore store;
  final http.Client _client;
  Uri? origin;
  Map<String, dynamic>? user;
  String? _access;
  String? _refresh;
  String? _deviceId;
  DateTime? _expires;
  // 토큰 회전 중 들어오는 요청들이 같은 Future를 기다리도록 보관한다.
  Future<void>? _refreshing;
  bool restoring = true;
  // 비동기 요청을 시작한 로그인 세션이 아직 유효한지 판별하는 세대 번호다.
  int _generation = 0;
  bool get signedIn => user != null && _refresh != null;
  String? get userId => user?['id']?.toString();
  bool get isAdmin => user?['role'] == 'admin';
  String? get refreshToken => _refresh;
  String? get deviceId => _deviceId;

  /// 로그인마다 새 식별자를 발급해 이전 로그인에 연결된 푸시를 구분한다.
  static String _newDeviceId() {
    final random = Random.secure();
    return List.generate(
      16,
      (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
    ).join();
  }

  /// 저장된 세션을 복원하며 읽기 실패나 손상된 데이터는 비로그인 상태로 처리한다.
  /// 저장소가 응답하지 않아도 시작 화면에 머물지 않도록 읽기 시간을 제한한다.
  Future<void> restore() async {
    try {
      final raw = await store
          .read('session')
          .timeout(const Duration(seconds: 5));
      if (raw != null) {
        final session = jsonDecode(raw) as Map<String, dynamic>;
        origin = ApiConfig.parseOrigin(session['origin'] as String);
        _access = session['access_token'] as String;
        _refresh = session['refresh_token'] as String;
        _deviceId = session['device_id'] as String? ?? _newDeviceId();
        user = Map<String, dynamic>.from(session['user'] as Map);
        _expires = DateTime.parse(session['expires_at'] as String);
      }
    } catch (_) {
      _access = null;
      _refresh = null;
      user = null;
    } finally {
      restoring = false;
      notifyListeners();
    }
  }

  /// 버전이 명시된 API 경로로 JSON 요청을 보내고 연결 실패를 사용자용 오류로 바꾼다.
  /// 로그인·갱신에도 사용하므로 인증 토큰은 호출자가 필요한 경우에만 전달한다.
  Future<http.Response> _raw(
    String method,
    String path, {
    Map<String, String>? query,
    Object? body,
    String? token,
    Uri? target,
    Duration timeout = const Duration(seconds: 20),
  }) async {
    final base = target ?? origin;
    if (base == null) throw const ApiException(0, '서버 주소를 설정하세요.');
    if (!path.startsWith('/api/v1/')) {
      throw ArgumentError('Expected a versioned API path');
    }
    final uri = base.resolve(path).replace(queryParameters: query);
    // 리다이렉트를 자동 추적하지 않아 인증 헤더를 다른 주소로 재전송하지 않는다.
    final request = http.Request(method, uri)
      ..followRedirects = false
      ..headers['Accept'] = 'application/json';
    if (token != null) request.headers['Authorization'] = 'Bearer $token';
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    try {
      return await (() async {
        final response = await _client.send(request);
        return http.Response.fromStream(response);
      })().timeout(timeout);
    } on TimeoutException {
      throw const ApiException(0, '서버 응답 시간이 초과되었습니다. 다시 시도하세요.');
    } on http.ClientException {
      throw const ApiException(0, '서버 연결을 확인하세요.');
    }
  }

  /// 성공 응답은 JSON 객체로 해석하고 HTTP 오류는 응답 본문 대신 정해진 안내로 바꾼다.
  Map<String, dynamic> _decode(http.Response response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final message = switch (response.statusCode) {
        401 => '로그인이 필요하거나 로그인 정보가 올바르지 않습니다.',
        403 => '이 항목에 접근할 권한이 없습니다.',
        404 => '항목을 찾을 수 없습니다.',
        409 => '현재 상태에서는 요청을 적용할 수 없습니다.',
        429 => '요청이 많습니다. 잠시 후 다시 시도하세요.',
        _ => '요청을 처리하지 못했습니다. (${response.statusCode})',
      };
      throw ApiException(response.statusCode, message);
    }
    if (response.bodyBytes.isEmpty) return {};
    final value = jsonDecode(utf8.decode(response.bodyBytes));
    if (value is! Map<String, dynamic>) {
      throw const ApiException(0, '서버 응답 형식이 올바르지 않습니다.');
    }
    return value;
  }

  /// 검증한 서버에서 인증받은 뒤 토큰과 사용자 정보를 저장하고 화면 전환을 알린다.
  Future<void> login(String server, String username, String password) async {
    final target = ApiConfig.parseOrigin(server);
    final result = _decode(
      await _raw(
        'POST',
        '/api/v1/auth/login',
        target: target,
        body: {'username': username.trim(), 'password': password},
      ),
    );
    _generation++;
    _deviceId = _newDeviceId();
    origin = target;
    await _setSession(result);
    restoring = false;
    notifyListeners();
  }

  /// 토큰 저장이 성공한 뒤 메모리의 세션을 교체한다. 비밀번호는 저장 대상에 포함하지 않는다.
  Future<void> _setSession(Map<String, dynamic> value) async {
    final access = value['access_token'] as String;
    final refresh = value['refresh_token'] as String;
    final sessionUser = Map<String, dynamic>.from(value['user'] as Map);
    final expiry = DateTime.now().toUtc().add(
      Duration(seconds: value['expires_in'] as int),
    );
    await store.write(
      'session',
      jsonEncode({
        'origin': origin.toString(),
        'access_token': access,
        'refresh_token': refresh,
        'device_id': _deviceId,
        'user': sessionUser,
        'expires_at': expiry.toIso8601String(),
      }),
    );
    _access = access;
    _refresh = refresh;
    user = sessionUser;
    _expires = expiry;
  }

  /// 여러 호출자가 하나의 refresh 요청을 공유하고 완료 후 다음 갱신을 허용한다.
  Future<void> refreshSession() async {
    // 서버가 refresh 토큰을 교체하므로 동시 요청은 하나의 갱신 결과를 공유한다.
    if (_refreshing != null) return _refreshing!;
    final future = _performRefresh();
    _refreshing = future;
    try {
      await future;
    } finally {
      if (identical(_refreshing, future)) _refreshing = null;
    }
  }

  /// 현재 세션의 토큰을 회전하고 서버가 갱신 자격을 거부한 경우에만 세션을 비운다.
  Future<void> _performRefresh() async {
    final generation = _generation;
    // 로그인/로그아웃마다 세대 번호가 바뀐다. 이전 계정의 늦은 응답을 새 세션에 적용하지 않는다.
    final refresh = _refresh;
    if (refresh == null) throw const ApiException(401, '로그인이 필요합니다.');
    try {
      final value = _decode(
        await _raw(
          'POST',
          '/api/v1/auth/refresh',
          body: {'refresh_token': refresh},
        ),
      );
      if (_generation != generation) {
        throw const ApiException(401, '세션이 변경되었습니다.');
      }
      await _setSession(value);
    } on ApiException catch (error) {
      if (error.statusCode == 401 && _generation == generation) {
        await _clearSession();
      }
      rethrow;
    }
  }

  /// 전송 중 만료될 가능성을 줄이기 위해 만료 60초 전부터 access 토큰을 갱신한다.
  Future<String> accessToken() async {
    if (!signedIn) throw const ApiException(401, '로그인이 필요합니다.');
    if (_expires == null ||
        _expires!.isBefore(
          DateTime.now().toUtc().add(const Duration(seconds: 60)),
        )) {
      await refreshSession();
    }
    return _access!;
  }

  /// 인증 요청을 실행하고 401이면 갱신한 토큰으로 한 번 재시도한다.
  /// 요청 도중 로그인 세션이 바뀌면 이전 응답을 호출자에게 전달하지 않는다.
  Future<Map<String, dynamic>> request(
    String method,
    String path, {
    Map<String, String>? query,
    Object? body,
  }) async {
    final generation = _generation;
    final token = await accessToken();
    // accessToken이 refresh 토큰도 회전했을 수 있어 본문에 실을 토큰을 다시 읽는다.
    if (body is Map<String, dynamic> && body.containsKey('refresh_token')) {
      body = {...body, 'refresh_token': _refresh};
    }
    // 품질 변경은 Edge 적용 결과를 기다리므로 일반 조회보다 긴 응답 시간을 허용한다.
    final timeout = method == 'PATCH' && path.endsWith('/video-profile')
        ? const Duration(seconds: 90)
        : const Duration(seconds: 20);
    var response = await _raw(
      method,
      path,
      query: query,
      body: body,
      token: token,
      timeout: timeout,
    );
    if (_generation != generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
    if (response.statusCode == 401) {
      // 다른 요청이 이미 토큰을 갱신했다면 재사용하고, 인증 실패 재시도는 한 번만 한다.
      if (_access == token) await refreshSession();
      if (body is Map<String, dynamic> && body.containsKey('refresh_token')) {
        body = {...body, 'refresh_token': _refresh};
      }
      response = await _raw(
        method,
        path,
        query: query,
        body: body,
        token: _access,
        timeout: timeout,
      );
      if (response.statusCode == 401) await _clearSession();
    }
    if (_generation != generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
    return _decode(response);
  }

  /// 빈 페이지가 나올 때까지 실제 수신 개수만큼 offset을 늘려 전체 목록을 모은다.
  Future<List<Map<String, dynamic>>> listAll(
    String path, {
    Map<String, String> query = const {},
  }) async {
    final items = <Map<String, dynamic>>[];
    var offset = 0;
    while (true) {
      final page = await request(
        'GET',
        path,
        query: {...query, 'limit': '100', 'offset': '$offset'},
      );
      final batch = (page['items'] as List).cast<Map<String, dynamic>>();
      if (batch.isEmpty) break;
      items.addAll(batch);
      offset += batch.length;
    }
    return items;
  }

  /// 로그인 서버를 기준으로 인증 헤더를 사용할 수 있는 영상 URL인지 검증한다.
  Uri mediaUri(String value) => ApiConfig.mediaUri(origin!, value);

  /// 진행 중인 토큰 회전 후 서버에서 세션을 폐기한다. 실패하면 재시도할 세션을 유지한다.
  Future<void> logout() async {
    if (_refreshing != null) await _refreshing;
    _decode(
      await _raw(
        'POST',
        '/api/v1/auth/logout',
        token: _access,
        body: {'refresh_token': _refresh},
      ),
    );
    await _clearSession();
  }

  /// 진행 중인 이전 요청을 무효화하고 메모리와 저장소에서 로그인 정보를 제거한다.
  Future<void> _clearSession() async {
    _generation++;
    _access = null;
    _refresh = null;
    _deviceId = null;
    _expires = null;
    user = null;
    await store.write('session', null);
    notifyListeners();
  }

  @override
  void dispose() {
    _client.close();
    super.dispose();
  }
}
