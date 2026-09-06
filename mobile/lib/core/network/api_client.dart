import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../config/api_config.dart';
import 'session_store.dart';

class ApiException implements Exception {
  const ApiException(this.statusCode, this.message);
  final int statusCode;
  final String message;
  @override
  String toString() => message;
}

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
  Future<void>? _refreshing;
  bool restoring = true;
  int _generation = 0;
  bool get signedIn => user != null && _refresh != null;
  String? get userId => user?['id']?.toString();
  bool get isAdmin => user?['role'] == 'admin';
  String? get refreshToken => _refresh;
  String? get deviceId => _deviceId;

  static String _newDeviceId() {
    final random = Random.secure();
    return List.generate(
      16,
      (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
    ).join();
  }

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

  Future<void> refreshSession() async {
    if (_refreshing != null) return _refreshing!;
    final future = _performRefresh();
    _refreshing = future;
    try {
      await future;
    } finally {
      if (identical(_refreshing, future)) _refreshing = null;
    }
  }

  Future<void> _performRefresh() async {
    final generation = _generation;
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

  Future<Map<String, dynamic>> request(
    String method,
    String path, {
    Map<String, String>? query,
    Object? body,
  }) async {
    final generation = _generation;
    final token = await accessToken();
    if (body is Map<String, dynamic> && body.containsKey('refresh_token')) {
      body = {...body, 'refresh_token': _refresh};
    }
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

  Uri mediaUri(String value) => ApiConfig.mediaUri(origin!, value);

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
