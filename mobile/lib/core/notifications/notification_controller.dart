// Android 푸시 등록·수신·화면 이동을 관리한다. Firebase 미설정 시 서버 조회는 유지한다.
import 'dart:async';
import 'dart:collection';
import 'dart:convert';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import '../network/api_client.dart';
import 'local_notification_service.dart';
import 'notification_payload.dart';

@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  // 표시는 OS에 맡기고 상세는 인증 후 조회한다. 인증값과 사건 내용은 로그에 남기지 않는다.
  try {
    await Firebase.initializeApp();
  } catch (_) {
    // Firebase가 없어도 백그라운드 처리를 종료한다.
  }
}

class NotificationController extends ChangeNotifier
    with WidgetsBindingObserver {
  NotificationController(
    this.api, {
    required this.onEvent,
    required this.onResume,
  }) {
    api.addListener(_authChanged);
    WidgetsBinding.instance.addObserver(this);
  }
  final ApiClient api;
  final void Function(NotificationPayload payload, bool open) onEvent;
  final VoidCallback onResume;
  final List<StreamSubscription> _subscriptions = [];
  final LinkedHashSet<String> _seen = LinkedHashSet();
  String? get deviceId => api.deviceId;
  String? _token;
  String? _session;
  bool enabled = true;
  bool allEvents = true;
  bool ready = false;
  bool registered = false;
  String status = '알림 준비 중';
  NotificationPayload? _pending;
  Future<void>? _syncing;
  bool _syncRequested = false;
  Timer? _retry;
  bool _disposed = false;
  bool _starting = false;

  static const attentionTypes = [
    'person_detected',
    'person_appeared',
    'camera_input_lost',
    'central_connection_lost',
    'inference_stream_lost',
    'external_power_lost',
    'battery_low',
    'battery_critical',
    'storage_warning',
    'storage_critical',
    'edge_offline',
    'video_profile_change_failed',
    'network_failure',
  ];

  void _update(String value) {
    if (_disposed) return;
    status = value;
    notifyListeners();
  }

  Future<void> start() async {
    if (_disposed || _starting || ready) return;
    _starting = true;
    try {
      enabled =
          (await api.store
              .read('push_enabled')
              .timeout(const Duration(seconds: 5))) !=
          'false';
      allEvents =
          (await api.store
              .read('push_all_events')
              .timeout(const Duration(seconds: 5))) !=
          'false';
      if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) {
        _update('푸시 알림은 Android에서 지원합니다.');
        return;
      }
      await LocalNotificationService.initialize(onTap: _localTap);
      await Firebase.initializeApp();
      FirebaseMessaging.onBackgroundMessage(firebaseMessagingBackgroundHandler);
      _subscriptions.add(
        FirebaseMessaging.onMessage.listen(
          (message) => handleForeground(message.data),
        ),
      );
      _subscriptions.add(
        FirebaseMessaging.onMessageOpenedApp.listen((m) => openEvent(m.data)),
      );
      _subscriptions.add(
        FirebaseMessaging.instance.onTokenRefresh.listen((token) {
          _token = token;
          unawaited(sync());
        }),
      );
      ready = true;
      final initial = await FirebaseMessaging.instance.getInitialMessage();
      if (initial != null) openEvent(initial.data);
      final local = await LocalNotificationService.launchPayload();
      if (local != null) _localTap(local);
      _authChanged();
      await sync();
    } catch (_) {
      ready = false;
      for (final subscription in _subscriptions) {
        await subscription.cancel();
      }
      _subscriptions.clear();
      _update('이 빌드에는 푸시 연결 설정이 없습니다. 서버 조회는 계속 사용할 수 있습니다.');
    } finally {
      _starting = false;
    }
  }

  void _authChanged() {
    // 같은 단말에서도 계정이나 서버가 바뀔 수 있어 이전 알림과 중복 수신 기록을 비운다.
    final current = api.signedIn
        ? '${api.origin}|${api.userId}|$deviceId'
        : null;
    if (current == _session) return;
    _session = current;
    registered = false;
    _seen.clear();
    unawaited(LocalNotificationService.clear());
    if (current == null) {
      _retry?.cancel();
      _update('로그인 후 알림을 받을 수 있습니다.');
    } else {
      unawaited(sync());
      final pending = _pending;
      _pending = null;
      if (pending != null && pending.belongsTo(api.userId, deviceId)) {
        onEvent(pending, true);
      }
    }
  }

  Future<void> setPreferences({
    required bool receive,
    required bool everyEvent,
  }) async {
    enabled = receive;
    allEvents = everyEvent;
    await api.store.write('push_enabled', '$enabled');
    await api.store.write('push_all_events', '$allEvents');
    // 이전 등록 요청이 새 수신 설정을 덮어쓰지 않도록 완료를 기다린 뒤 다시 등록한다.
    if (_syncing != null) await _syncing;
    await sync();
  }

  Future<void> sync() async {
    // 토큰 변경과 설정 변경이 겹치면 등록 요청을 직렬화하고 마지막 변경까지 반영한다.
    if (_disposed || !ready || !api.signedIn) return;
    _syncRequested = true;
    if (_syncing != null) return _syncing!;
    final future = () async {
      do {
        _syncRequested = false;
        await _sync();
      } while (_syncRequested && !_disposed && ready && api.signedIn);
    }();
    _syncing = future;
    try {
      await future;
    } finally {
      if (identical(_syncing, future)) _syncing = null;
    }
  }

  Future<void> _sync() async {
    final session = _session;
    try {
      final messaging = FirebaseMessaging.instance;
      final permission = enabled
          ? await messaging.requestPermission(
              alert: true,
              badge: true,
              sound: true,
            )
          : await messaging.getNotificationSettings();
      final permitted =
          permission.authorizationStatus == AuthorizationStatus.authorized ||
          permission.authorizationStatus == AuthorizationStatus.provisional;
      if (!enabled || !permitted) {
        await api.request('DELETE', '/api/v1/notifications/devices/$deviceId');
        registered = false;
        await LocalNotificationService.clear();
        _update(enabled ? '기기 설정에서 알림 권한을 허용하세요.' : '알림이 꺼져 있습니다.');
        return;
      }
      _token ??= await messaging.getToken().timeout(
        const Duration(seconds: 15),
      );
      if (_token == null) throw StateError('No registration token');
      if (session != _session || !api.signedIn) return;
      await api.request(
        'PUT',
        '/api/v1/notifications/devices',
        body: {
          'device_id': deviceId,
          'token': _token,
          'platform': 'android',
          'enabled': true,
          'refresh_token': api.refreshToken,
          'event_types': allEvents ? null : attentionTypes,
        },
      );
      final server = await api.request('GET', '/api/v1/notifications/status');
      if (session != _session) return;
      registered = true;
      _retry?.cancel();
      _update(
        server['enabled'] == true ? '알림 수신 준비 완료' : '중앙 서버의 푸시 설정을 기다리고 있습니다.',
      );
    } catch (_) {
      if (session != _session || !api.signedIn) return;
      registered = false;
      _update('알림 등록을 다시 시도하고 있습니다.');
      _retry?.cancel();
      _retry = Timer(const Duration(seconds: 30), () => unawaited(sync()));
    }
  }

  Future<void> handleForeground(Map<String, dynamic> data) async {
    // 현재 사용자·로그인 기기의 알림만 반영하고 같은 이벤트가 반복 도착하면 한 번만 표시한다.
    final payload = NotificationPayload.parse(data);
    if (!enabled ||
        payload == null ||
        !payload.belongsTo(api.userId, deviceId)) {
      return;
    }
    if (!_seen.add(payload.eventId)) return;
    if (_seen.length > 128) _seen.remove(_seen.first);
    onEvent(payload, false);
    try {
      await LocalNotificationService.showEvent(payload);
    } catch (_) {
      // 알림 표시가 실패해도 위에서 요청한 화면 갱신은 유지한다.
    }
  }

  void openEvent(Map<String, dynamic> data) {
    final payload = NotificationPayload.parse(data);
    if (payload == null) return;
    if (!api.signedIn) {
      // 앱 시작 직후에는 세션 복원이 끝나지 않을 수 있으므로 탭한 알림을 잠시 보관한다.
      _pending = payload;
      return;
    }
    if (payload.belongsTo(api.userId, deviceId)) onEvent(payload, true);
  }

  void _localTap(String? value) {
    if (value == null) return;
    try {
      openEvent(Map<String, dynamic>.from(jsonDecode(value) as Map));
    } catch (_) {
      // 형식이 잘못된 알림은 무시한다.
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && api.signedIn) {
      onResume();
      unawaited(sync());
    }
  }

  @override
  void dispose() {
    // 화면/앱 객체가 해제된 뒤 타이머와 스트림이 상태를 변경하지 않도록 연결을 정리한다.
    _disposed = true;
    _retry?.cancel();
    api.removeListener(_authChanged);
    WidgetsBinding.instance.removeObserver(this);
    for (final subscription in _subscriptions) {
      unawaited(subscription.cancel());
    }
    super.dispose();
  }
}
