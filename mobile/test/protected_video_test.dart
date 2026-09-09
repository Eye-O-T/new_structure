import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:video_player_platform_interface/video_player_platform_interface.dart';
import 'package:app/core/network/api_client.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/features/live/presentation/protected_video.dart';
import 'api_client_test.dart' show MemoryStore, json, session;

class _VideoPlatform extends VideoPlayerPlatform {
  final sources = <DataSource>[];
  final streams = <int, StreamController<VideoEvent>>{};
  final playing = <int>{};
  final disposed = <int>{};
  final positions = <int, Duration>{};
  final disposalGates = <int, Completer<void>>{};
  bool delayed = false;
  bool failCreation = false;
  @override
  Future<void> init() async {}
  @override
  Future<int> createWithOptions(VideoCreationOptions options) async {
    if (failCreation) {
      throw PlatformException(
        code: 'creation_failed',
        message: 'Cannot allocate player',
      );
    }
    final id = sources.length;
    sources.add(options.dataSource);
    streams[id] = StreamController<VideoEvent>(onCancel: () async {});
    if (!delayed) initialize(id);
    return id;
  }

  void initialize(int id) => streams[id]!.add(
    VideoEvent(
      eventType: VideoEventType.initialized,
      size: const Size(1600, 900),
      duration: const Duration(minutes: 10),
    ),
  );
  @override
  Stream<VideoEvent> videoEventsFor(int playerId) => streams[playerId]!.stream;
  @override
  Future<void> dispose(int playerId) async {
    disposed.add(playerId);
    playing.remove(playerId);
    await disposalGates[playerId]?.future;
  }

  @override
  Future<void> play(int playerId) async {
    playing.add(playerId);
  }

  @override
  Future<void> pause(int playerId) async {
    playing.remove(playerId);
  }

  @override
  Future<Duration> getPosition(int playerId) async =>
      const Duration(seconds: 2);
  @override
  Future<void> seekTo(int playerId, Duration position) async {
    positions[playerId] = position;
  }

  @override
  Future<void> setLooping(int playerId, bool looping) async {}
  @override
  Future<void> setVolume(int playerId, double volume) async {}
  @override
  Future<void> setMixWithOthers(bool mixWithOthers) async {}
  @override
  Future<void> setPlaybackSpeed(int playerId, double speed) async {}
  @override
  Future<void> setPreventsDisplaySleepDuringVideoPlayback(
    int playerId,
    bool preventsDisplaySleepDuringVideoPlayback,
  ) async {}
  @override
  Widget buildViewWithOptions(VideoViewOptions options) =>
      const SizedBox.expand();
}

void main() {
  late _VideoPlatform video;
  late ApiClient api;
  setUp(() async {
    video = _VideoPlatform();
    VideoPlayerPlatform.instance = video;
    api = ApiClient(
      store: MemoryStore(),
      client: MockClient((request) async {
        if (request.url.path.endsWith('/login')) return json(session());
        if (request.url.path.endsWith('/refresh')) {
          return json(session('changed'));
        }
        return json({'playback_url': '/${request.url.path.split('/')[4]}.mp4'});
      }),
    );
    await api.login('https://cctv.test', 'admin', 'password');
  });
  tearDown(() {
    api.dispose();
  });

  Widget widget([String id = '1']) => ProviderScope(
    overrides: [apiClientProvider.overrideWithValue(api)],
    child: MaterialApp(
      home: Scaffold(
        body: ProtectedVideo(
          endpoint: '/api/v1/recordings/$id/playback',
          urlField: 'playback_url',
        ),
      ),
    ),
  );

  testWidgets('token renewal and resume preserve user pause', (tester) async {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpWidget(widget());
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('일시 정지'));
    await tester.pump();
    await api.refreshSession();
    await tester.pump(const Duration(seconds: 30));
    await tester.pumpAndSettle();
    expect(video.sources, hasLength(2));
    expect(video.playing, isEmpty);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();
    expect(video.playing, isEmpty);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
  });

  testWidgets('initialization finishing in background stays paused', (
    tester,
  ) async {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    video.delayed = true;
    await tester.pumpWidget(widget());
    await tester.pump();
    expect(video.sources, hasLength(1));
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
    video.initialize(0);
    await tester.pumpAndSettle();
    expect(video.playing, isEmpty);
    await api.refreshSession();
    await tester.pump(const Duration(seconds: 30));
    expect(video.sources, hasLength(1));
    video.streams[0]!.addError(
      PlatformException(code: 'background_disconnect', message: 'Disconnected'),
    );
    await tester.pump();
    expect(video.sources, hasLength(1));
    video.delayed = false;
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();
    expect(video.playing, {1});
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
  });

  testWidgets('recording changed while inactive starts at its own beginning', (
    tester,
  ) async {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpWidget(widget());
    await tester.pumpAndSettle();
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await tester.pumpWidget(widget('2'));
    await tester.pump();
    expect(video.sources, hasLength(1));
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await tester.pumpAndSettle();
    expect(video.sources, hasLength(2));
    expect(video.sources.last.uri, endsWith('/2.mp4'));
    expect(video.positions[1], isNull);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
  });

  testWidgets('late initialization cannot replace a newer endpoint', (
    tester,
  ) async {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    video.delayed = true;
    await tester.pumpWidget(widget());
    await tester.pump();
    await tester.pumpWidget(widget('2'));
    await tester.pump();
    expect(video.sources, hasLength(2));
    video.initialize(1);
    await tester.pumpAndSettle();
    video.initialize(0);
    await tester.pumpAndSettle();
    expect(video.playing, {1});
    expect(video.disposed, contains(0));
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pump();
    expect(video.disposed, contains(1));
  });

  testWidgets(
    'native creation failure shows an error and permits a successful retry',
    (tester) async {
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      video.failCreation = true;
      await tester.pumpWidget(widget());
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.textContaining('영상을 재생하지 못했습니다.'), findsOneWidget);
      final retry = find.byWidgetPredicate(
        (value) => value is IconButton && value.tooltip == '영상 다시 연결',
      );
      expect(tester.widget<IconButton>(retry).onPressed, isNotNull);
      // 플러그인의 생성 completer가 완료되지 않아도 정리 대기 시간 뒤 재연결이 가능하다.
      await tester.pump(const Duration(seconds: 4));
      video.failCreation = false;
      await tester.tap(retry);
      await tester.pumpAndSettle();
      expect(video.playing, {0});
      expect(find.textContaining('영상을 재생하지 못했습니다.'), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
    },
  );

  testWidgets(
    'a stuck previous native disposal cannot block the replacement or its controls',
    (tester) async {
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpWidget(widget());
      await tester.pumpAndSettle();
      final disposal = Completer<void>();
      video.disposalGates[0] = disposal;
      await tester.pumpWidget(widget('2'));
      await tester.pumpAndSettle();
      expect(video.playing, {1});
      expect(find.byType(CircularProgressIndicator), findsNothing);
      await tester.tap(find.byTooltip('일시 정지'));
      await tester.pump();
      expect(video.playing, isEmpty);
      await tester.pump(const Duration(seconds: 4));
      disposal.completeError(PlatformException(code: 'late_disposal_failure'));
      await tester.pump();
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
    },
  );
}
