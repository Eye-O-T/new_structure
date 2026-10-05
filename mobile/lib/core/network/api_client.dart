// 모든 JSON API 요청의 로그인 토큰, 만료 갱신, 오류 변환을 한곳에서 처리한다.
// 영상 데이터 자체의 재생은 ProtectedVideo가 맡으며 이 클래스는 주소와 인증을 제공한다.
import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

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

/// 화면 선택이 바뀌면 진행 중인 HTTP 요청과 이어질 재시도를 중단한다.
class ApiCancellation {
  final _cancelled = Completer<void>();
  Future<void> get whenCancelled => _cancelled.future;
  bool get isCancelled => _cancelled.isCompleted;
  void cancel() {
    if (!isCancelled) _cancelled.complete();
  }

  void check() {
    if (isCancelled) throw const ApiRequestCancelled();
  }
}

class ApiRequestCancelled implements Exception {
  const ApiRequestCancelled();
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
  // 인증 응답과 저장소 변경을 같은 순서로 완료해 이전 저장이 새 세션을 덮지 않게 한다.
  Future<void>? _sessionOperations;
  ApiCancellation _sessionCancellation = ApiCancellation();
  Future<void>? _logoutCleanup;
  bool _needsSessionCleanup = false;
  bool _disposed = false;
  String? sessionCleanupError;
  bool get clearingSession => _logoutCleanup != null;
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

  /// 읽기·인증·저장·삭제가 완료될 때까지 다음 세션 변경을 대기시킨다.
  Future<T> _serializeSession<T>(Future<T> Function() operation) async {
    final previous = _sessionOperations;
    final completed = Completer<void>();
    _sessionOperations = completed.future;
    try {
      if (previous != null) await previous;
      return await operation();
    } finally {
      // 실패도 호출자에게 전달한 뒤 다음 작업을 허용한다. 대기 큐는 오류로 중단되지 않는다.
      completed.complete();
      if (identical(_sessionOperations, completed.future)) {
        _sessionOperations = null;
      }
    }
  }

  /// 저장된 세션을 복원하며 손상·읽기 실패는 비로그인 상태로 처리한다.
  /// 저장소가 응답하지 않아도 시작 화면에 머물지 않도록 읽기 시간을 제한한다.
  Future<void> restore() => _serializeSession(_restore);

  Future<void> _restore() async {
    final generation = _generation;
    try {
      final raw = await store
          .read('session')
          .timeout(const Duration(seconds: 5));
      if (generation != _generation || _needsSessionCleanup) return;
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
      if (generation == _generation) {
        _access = null;
        _refresh = null;
        user = null;
      }
    } finally {
      if (generation == _generation) {
        _generation++;
        restoring = false;
        if (!_disposed) notifyListeners();
      }
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
    ApiCancellation? cancellation,
    bool sessionBound = true,
  }) async {
    cancellation?.check();
    final base = target ?? origin;
    if (base == null) throw const ApiException(0, '서버 주소를 설정하세요.');
    if (!path.startsWith('/api/v1/')) {
      throw ArgumentError('Expected a versioned API path');
    }
    final uri = base.resolve(path).replace(queryParameters: query);
    final abort = ApiCancellation();
    final signals = <Future<void>>[
      if (sessionBound) _sessionCancellation.whenCancelled,
      if (cancellation != null) cancellation.whenCancelled,
    ];
    if (signals.isNotEmpty) {
      unawaited(Future.any(signals).then((_) => abort.cancel()));
    }
    // 리다이렉트를 자동 추적하지 않아 인증 헤더를 다른 주소로 재전송하지 않는다.
    final request =
        http.AbortableRequest(method, uri, abortTrigger: abort.whenCancelled)
          ..followRedirects = false
          ..headers['Accept'] = 'application/json';
    if (token != null) request.headers['Authorization'] = 'Bearer $token';
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    try {
      return await Future.any<http.Response>([
        (() async {
          final response = await _client.send(request);
          return http.Response.fromStream(response);
        })(),
        abort.whenCancelled.then((_) => throw const ApiRequestCancelled()),
      ]).timeout(timeout);
    } on http.RequestAbortedException {
      throw const ApiRequestCancelled();
    } on TimeoutException {
      abort.cancel();
      throw const ApiException(0, '서버 응답 시간이 초과되었습니다. 다시 시도하세요.');
    } on http.ClientException {
      throw const ApiException(0, '서버 연결을 확인하세요.');
    }
  }

  /// 성공 응답은 JSON 객체로 해석하고 HTTP 오류는 서버 응답 메시지를 보존한다.
  Map<String, dynamic> _decode(http.Response response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final raw = utf8.decode(response.bodyBytes, allowMalformed: true);
      String message = raw;
      try {
        final value = jsonDecode(raw);
        if (value is Map<String, dynamic>) {
          final error = value['error'];
          final nested = error is Map<String, dynamic>
              ? error['message']
              : null;
          final detail = value['detail'];
          if (nested is String && nested.isNotEmpty) {
            message = nested;
          } else if (detail is String && detail.isNotEmpty) {
            message = detail;
          } else if (detail != null) {
            message = jsonEncode(detail);
          } else {
            message = jsonEncode(value);
          }
        }
      } on FormatException {
        // Keep the raw response body for non-JSON development responses.
      }
      if (message.isEmpty) message = 'HTTP ${response.statusCode}';
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
  Future<void> login(String server, String username, String password) {
    if (_needsSessionCleanup) {
      return Future.error(
        const ApiException(0, '저장된 로그인 정보 삭제를 완료한 뒤 다시 로그인하세요.'),
      );
    }
    final generation = _generation;
    return _serializeSession(() {
      if (_needsSessionCleanup) {
        throw const ApiException(0, '저장된 로그인 정보 삭제를 완료한 뒤 다시 로그인하세요.');
      }
      if (generation != _generation) {
        throw const ApiException(401, '세션이 변경되었습니다.');
      }
      return _login(server, username, password);
    });
  }

  Future<void> _login(String server, String username, String password) async {
    final generation = _generation;
    final target = ApiConfig.parseOrigin(server);
    final result = _decode(
      await _raw(
        'POST',
        '/api/v1/auth/login',
        target: target,
        body: {'username': username.trim(), 'password': password},
      ),
    );
    if (generation != _generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
    await _setSession(
      result,
      target: target,
      device: _newDeviceId(),
      newLogin: true,
    );
    restoring = false;
    notifyListeners();
  }

  /// 토큰 저장이 성공한 뒤 메모리의 세션을 교체한다. 비밀번호는 저장 대상에 포함하지 않는다.
  Future<void> _setSession(
    Map<String, dynamic> value, {
    required Uri target,
    required String device,
    bool newLogin = false,
  }) async {
    final generation = _generation;
    final access = value['access_token'] as String;
    final refresh = value['refresh_token'] as String;
    final sessionUser = Map<String, dynamic>.from(value['user'] as Map);
    final expiry = DateTime.now().toUtc().add(
      Duration(seconds: value['expires_in'] as int),
    );
    await store.write(
      'session',
      jsonEncode({
        'origin': target.toString(),
        'access_token': access,
        'refresh_token': refresh,
        'device_id': device,
        'user': sessionUser,
        'expires_at': expiry.toIso8601String(),
      }),
    );
    if (generation != _generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
    // 저장이 끝날 때 origin과 자격 증명을 함께 교체한다. 대기 중에는 이전 세션만 보인다.
    if (newLogin) _generation++;
    origin = target;
    _deviceId = device;
    _access = access;
    _refresh = refresh;
    user = sessionUser;
    _expires = expiry;
  }

  /// 여러 호출자가 하나의 refresh 요청을 공유하고 완료 후 다음 갱신을 허용한다.
  Future<void> refreshSession() async {
    // 서버가 refresh 토큰을 교체하므로 동시 요청은 하나의 갱신 결과를 공유한다.
    if (_refreshing != null) return _refreshing!;
    final generation = _generation;
    final future = _serializeSession(() async {
      if (_generation != generation) {
        throw const ApiException(401, '세션이 변경되었습니다.');
      }
      await _performRefresh();
    });
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
      await _setSession(value, target: origin!, device: _deviceId!);
    } on ApiException catch (error) {
      if (error.statusCode == 401 && _generation == generation) {
        await _clearSession();
      }
      rethrow;
    }
  }

  /// 전송 중 만료될 가능성을 줄이기 위해 만료 60초 전부터 access 토큰을 갱신한다.
  Future<String> accessToken() async {
    final generation = _generation;
    if (!signedIn) throw const ApiException(401, '로그인이 필요합니다.');
    if (_expires == null ||
        _expires!.isBefore(
          DateTime.now().toUtc().add(const Duration(seconds: 60)),
        )) {
      await refreshSession();
    }
    if (_generation != generation || !signedIn) {
      throw const ApiException(401, '세션이 변경되었습니다.');
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
    ApiCancellation? cancellation,
  }) async {
    cancellation?.check();
    final generation = _generation;
    final token = await accessToken();
    cancellation?.check();
    if (_generation != generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
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
      cancellation: cancellation,
    );
    cancellation?.check();
    if (_generation != generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
    if (response.statusCode == 401) {
      // 다른 요청이 이미 토큰을 갱신했다면 재사용하고, 인증 실패 재시도는 한 번만 한다.
      if (_access == token) await refreshSession();
      cancellation?.check();
      if (_generation != generation) {
        throw const ApiException(401, '세션이 변경되었습니다.');
      }
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
        cancellation: cancellation,
      );
      cancellation?.check();
      if (_generation != generation) {
        throw const ApiException(401, '세션이 변경되었습니다.');
      }
      if (response.statusCode == 401) {
        await _serializeSession(() async {
          if (_generation == generation) await _clearSession();
        });
      }
    }
    if (_generation != generation) {
      throw const ApiException(401, '세션이 변경되었습니다.');
    }
    return _decode(response);
  }

  Future<Uint8List> getBytes(String path, {ApiCancellation? cancellation}) async {
    final generation = _generation;
    final token = await accessToken();
    var response = await _raw('GET', path, token: token, cancellation: cancellation);
    if (response.statusCode == 401 && _access == token) {
      await refreshSession();
      response = await _raw('GET', path, token: _access, cancellation: cancellation);
    }
    if (_generation != generation) throw const ApiException(401, 'Session changed.');
    if (response.statusCode < 200 || response.statusCode >= 300) {
      _decode(response);
    }
    return response.bodyBytes;
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

  /// 화면과 이전 요청은 즉시 로그아웃하고 서버 폐기는 별도로 시도한다.
  Future<void> logout() async {
    if (_logoutCleanup == null) {
      final target = origin;
      final access = _access;
      final refresh = _refresh;
      _invalidateSession();
      // 실제 저장소 작업은 끝날 때까지 직렬화한다. timeout만으로 잠금을 풀면
      // 늦게 끝난 이전 저장/삭제가 다음 로그인의 정보를 덮어쓸 수 있다.
      final cleanup = _serializeSession(_deleteStoredSession);
      _logoutCleanup = cleanup;
      unawaited(
        cleanup.then(
          (_) => _finishCleanup(cleanup),
          onError: (Object _, StackTrace _) => _finishCleanup(cleanup),
        ),
      );
      notifyListeners();
      if (target != null && (access != null || refresh != null)) {
        unawaited(_revokeSession(target, access, refresh));
      }
    }
    try {
      await _logoutCleanup!.timeout(const Duration(seconds: 5));
    } catch (_) {
      sessionCleanupError =
          '휴대폰의 로그인 정보 삭제를 완료하지 못했습니다. '
          '앱을 다시 열면 이전 로그인이 복원될 수 있으니 삭제를 다시 시도하세요.';
      if (!_disposed) notifyListeners();
      throw ApiException(0, sessionCleanupError!);
    }
  }

  void _finishCleanup(Future<void> cleanup) {
    if (identical(_logoutCleanup, cleanup)) _logoutCleanup = null;
    if (!_disposed) notifyListeners();
  }

  Future<void> _revokeSession(
    Uri target,
    String? access,
    String? refresh,
  ) async {
    try {
      _decode(
        await _raw(
          'POST',
          '/api/v1/auth/logout',
          target: target,
          token: access,
          body: {'refresh_token': refresh},
          timeout: const Duration(seconds: 3),
          sessionBound: false,
        ),
      );
    } catch (_) {
      // 서버에 연결할 수 없어도 앱의 로그아웃을 되돌리지 않는다.
    }
  }

  void _invalidateSession() {
    _generation++;
    _sessionCancellation.cancel();
    _sessionCancellation = ApiCancellation();
    _refreshing = null;
    _access = null;
    _refresh = null;
    _deviceId = null;
    _expires = null;
    user = null;
    restoring = false;
    _needsSessionCleanup = true;
    sessionCleanupError = null;
  }

  Future<void> _deleteStoredSession() async {
    await store.write('session', null);
    _needsSessionCleanup = false;
    sessionCleanupError = null;
  }

  /// 인증 거절 처리에서는 이미 세션 대기열 안이므로 직접 삭제한다.
  Future<void> _clearSession() async {
    _invalidateSession();
    notifyListeners();
    try {
      await _deleteStoredSession();
    } catch (_) {
      sessionCleanupError = '저장된 로그인 정보를 삭제하지 못했습니다. 삭제를 다시 시도하세요.';
      rethrow;
    } finally {
      if (!_disposed) notifyListeners();
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _sessionCancellation.cancel();
    _client.close();
    super.dispose();
  }
}
