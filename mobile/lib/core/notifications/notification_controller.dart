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

/// Firebase가 별도 실행 문맥에서 호출하므로 최적화 시에도 진입점을 유지한다.
@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  // 표시는 OS에 맡기고 상세는 인증 후 조회한다. 인증값과 사건 내용은 로그에 남기지 않는다.
  try {
    await Firebase.initializeApp();
  } catch (_) {
    // Firebase가 없어도 백그라운드 처리를 종료한다.
  }
}

/// 로그인 세션, OS 권한, FCM 토큰을 서버의 기기 등록 상태와 연결한다.
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
  /// open이 true면 알림 탭에 따른 상세 이동, false면 전경 수신에 따른 조회 갱신이다.
  final void Function(NotificationPayload payload, bool open) onEvent;
  final VoidCallback onResume;
  final List<StreamSubscription> _subscriptions = [];
  // 삽입 순서를 유지해 중복 이벤트 기록의 크기를 제한할 때 가장 오래된 ID부터 지운다.
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

  /// 모든 이벤트 수신을 끈 경우 서버에 등록할 사람 감지·장애·위험 이벤트 목록이다.
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

  /// 해제된 관리자의 UI 통지는 막고 현재 연결 상태를 설정 화면에 전달한다.
  void _update(String value) {
    if (_disposed) return;
    status = value;
    notifyListeners();
  }

  /// 설정을 복원하고 Android 푸시 구독과 앱을 시작한 알림을 연결한다.
  /// 초기화 실패 시 구독을 정리해 설정 화면에서 다시 초기화할 수 있게 한다.
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

  /// 로그인 대상이 바뀌면 수신 기록을 초기화하고 보류한 탭의 수신 대상을 다시 확인한다.
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

  /// 선택한 수신 범위를 저장한 뒤 서버의 기기 등록에도 같은 설정을 반영한다.
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

  /// 등록 실행 중 추가 변경이 들어오면 현재 요청 뒤에 다시 동기화한다.
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

  /// OS 권한과 수신 설정에 따라 기기를 등록·해제하고 실패 시 30초 뒤 재시도한다.
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
      // 권한 확인이나 토큰 발급을 기다리는 사이 계정이 바뀌면 이전 등록 작업을 중단한다.
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

  /// 전경 수신을 목록 갱신과 로컬 알림으로 전달하며 중복 ID는 최근 128개까지 기억한다.
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

  /// 로그인 전 탭은 보류하고 로그인 후에는 현재 수신 대상의 상세 화면만 연다.
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

  /// 로컬 알림 문자열도 원격 알림과 같은 payload 검증과 로그인 검사에 통과시킨다.
  void _localTap(String? value) {
    if (value == null) return;
    try {
      openEvent(Map<String, dynamic>.from(jsonDecode(value) as Map));
    } catch (_) {
      // 형식이 잘못된 알림은 무시한다.
    }
  }

  /// 앱 복귀 시 조회 결과와 OS 설정에서 바뀌었을 수 있는 알림 권한을 다시 확인한다.
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
