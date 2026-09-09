// 토큰 갱신 시 플레이어를 다시 만들어 HLS 목록·조각 요청에도 새 인증 헤더를 적용한다.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';
import 'package:app/core/network/providers.dart';
import 'object_overlay.dart';

/// 인증 API에서 재생 주소를 받아 토큰을 포함한 실시간·녹화 플레이어를 구성한다.
class ProtectedVideo extends ConsumerStatefulWidget {
  const ProtectedVideo({
    super.key,
    required this.endpoint,
    required this.urlField,
    this.live = false,
    this.cameraId,
  });

  /// 재생 URL 자체가 아니라 URL 정보를 반환하는 인증 API 경로다.
  final String endpoint;

  /// 실시간과 녹화 API가 서로 다른 응답 필드에 URL을 제공하므로 호출자가 지정한다.
  final String urlField;
  final bool live;
  final String? cameraId;
  @override
  ConsumerState<ProtectedVideo> createState() => _ProtectedVideoState();
}

class _ProtectedVideoState extends ConsumerState<ProtectedVideo>
    with WidgetsBindingObserver {
  VideoPlayerController? _player;
  Timer? _timer;
  String? _token;
  String? _playerEndpoint;
  String? _error;
  bool _loading = false;
  // 플레이어 오류의 자동 복구는 한 번만 하며 수동 재연결이나 endpoint 변경 때 초기화한다.
  bool _retried = false;
  bool _showObjects = true;
  bool _foreground = true;
  bool _wantPlaying = true;
  int _loadGeneration = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    _foreground = lifecycle == null || lifecycle == AppLifecycleState.resumed;
    unawaited(_load());
    _timer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => unawaited(_renew()),
    );
  }

  @override
  void didUpdateWidget(ProtectedVideo oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.endpoint != widget.endpoint ||
        oldWidget.urlField != widget.urlField) {
      _retried = false;
      unawaited(_load(resetPosition: true));
    }
  }

  /// 주기적으로 access 토큰을 확인하고 헤더가 바뀌었을 때만 플레이어를 재생성한다.
  Future<void> _renew() async {
    if (!mounted || _loading || !_foreground) return;
    final generation = _loadGeneration;
    try {
      final token = await ref.read(apiClientProvider).accessToken();
      if (!mounted ||
          !_foreground ||
          _loading ||
          generation != _loadGeneration) {
        return;
      }
      if (token != _token) await _load();
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    }
  }

  /// 새 요청이 이전 연결을 대체하며 늦게 준비된 플레이어는 세대 검사 후 해제한다.
  /// refresh는 플레이어 오류 복구 시 세션 갱신을 먼저 수행하도록 한다.
  Future<void> _load({bool refresh = false, bool resetPosition = false}) async {
    if (!mounted) return;
    final generation = ++_loadGeneration;
    if (!_foreground) {
      // 선택이 바뀌면 이전 초기화를 무효화하되 실제 연결은 앱 복귀 때 시작한다.
      setState(() => _loading = false);
      return;
    }
    final endpoint = widget.endpoint;
    final urlField = widget.urlField;
    bool current() => mounted && generation == _loadGeneration;
    _loading = true;
    if (mounted) setState(() => _error = null);
    VideoPlayerController? next;
    final old = _player;
    if (resetPosition) unawaited(old?.pause());
    // 녹화는 보던 위치를 이어 가고 실시간 영상은 새 연결의 현재 지점에서 시작한다.
    final position = widget.live || resetPosition || _playerEndpoint != endpoint
        ? Duration.zero
        : old?.value.position ?? Duration.zero;
    try {
      final api = ref.read(apiClientProvider);
      if (refresh) await api.refreshSession();
      if (!current()) return;
      final descriptor = await api.request('GET', endpoint);
      if (!current()) return;
      final uri = api.mediaUri(descriptor[urlField] as String);
      final token = await api.accessToken();
      if (!current()) return;
      next = VideoPlayerController.networkUrl(
        uri,
        httpHeaders: {'Authorization': 'Bearer $token'},
        // 앱 상태는 이 위젯에서 관리한다. 생성 실패 시 플러그인 관찰자가 남지 않게 한다.
        videoPlayerOptions: VideoPlayerOptions(allowBackgroundPlayback: true),
      );
      await next.initialize().timeout(const Duration(seconds: 25));
      // 초기화 대기 중 화면이 닫혔다면 새 플레이어를 연결하지 않고 즉시 정리한다.
      if (!current()) return;
      if (position > Duration.zero) await next.seekTo(position);
      if (!current()) return;
      if (_foreground && _wantPlaying) await next.play();
      if (!current()) return;
      // play/seek의 네이티브 응답을 기다리는 동안 앱 상태나 사용자 의도가 바뀔 수 있다.
      if (!_foreground || !_wantPlaying) await next.pause();
      if (!current()) return;
      // 새 플레이어가 준비된 뒤 이전 리스너와 자원을 정리해 활성 플레이어를 교체한다.
      _token = token;
      _player = next;
      _playerEndpoint = endpoint;
      next.addListener(_playerChanged);
      old?.removeListener(_playerChanged);
      unawaited(_disposePlayer(old));
    } catch (_) {
      if (current()) _error = '영상을 재생하지 못했습니다. 카메라와 서버 연결을 확인하세요.';
    } finally {
      if (current()) {
        _loading = false;
        setState(() {});
      }
      // 네이티브 생성 실패는 dispose까지 멈출 수 있다. UI 복구는 정리를 기다리지 않는다.
      if (next != null && next != _player) unawaited(_disposePlayer(next));
    }
  }

  Future<void> _disposePlayer(VideoPlayerController? player) async {
    if (player == null) return;
    try {
      await player.dispose().timeout(const Duration(seconds: 3));
    } catch (_) {
      // 늦게 끝나는 네이티브 정리는 계속 진행되지만 화면이나 다음 연결을 막지 않는다.
    }
  }

  /// 네이티브 플레이어의 오류를 감지해 토큰 갱신을 포함한 복구를 한 번 시도한다.
  void _playerChanged() {
    if (!mounted || !_foreground) return;
    if (_player?.value.hasError == true && !_loading && !_retried) {
      _retried = true;
      unawaited(_load(refresh: true));
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (mounted) {
      setState(() => _foreground = state == AppLifecycleState.resumed);
    }
    if (state == AppLifecycleState.resumed) {
      // 앱 복귀 시 연결을 새로 만들고, 화면을 벗어나면 재생을 일시 정지한다.
      unawaited(_load());
    } else {
      unawaited(_player?.pause());
    }
  }

  @override
  Widget build(BuildContext context) {
    final player = _player;
    return Column(
      children: [
        AspectRatio(
          aspectRatio: player?.value.isInitialized == true
              ? player!.value.aspectRatio
              : 16 / 9,
          child: ColoredBox(
            color: Colors.black,
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : _error != null
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Text(
                        _error!,
                        style: const TextStyle(color: Colors.white),
                      ),
                    ),
                  )
                : player == null
                ? const SizedBox.shrink()
                : Stack(
                    fit: StackFit.expand,
                    children: [
                      VideoPlayer(player),
                      // 재생 중인 전경 화면에서만 최신 좌표를 표시해 멈춘 영상 위의 박스를 피한다.
                      if (widget.live &&
                          widget.cameraId != null &&
                          _showObjects &&
                          _foreground)
                        ValueListenableBuilder<VideoPlayerValue>(
                          valueListenable: player,
                          builder: (_, value, _) =>
                              value.isPlaying &&
                                  !value.isBuffering &&
                                  !value.hasError
                              ? ObjectOverlay(cameraId: widget.cameraId!)
                              : const SizedBox.shrink(),
                        ),
                    ],
                  ),
          ),
        ),
        if (widget.live && widget.cameraId != null)
          SwitchListTile(
            dense: true,
            title: const Text('사람 위치·ID 표시'),
            subtitle: const Text('AI 감지 위치는 영상과 시차가 있을 수 있습니다.'),
            value: _showObjects,
            onChanged: (value) => setState(() => _showObjects = value),
          ),
        Row(
          children: [
            if (player != null)
              ValueListenableBuilder<VideoPlayerValue>(
                valueListenable: player,
                builder: (_, value, _) => IconButton(
                  tooltip: value.isPlaying ? '일시 정지' : '재생',
                  onPressed: () {
                    _wantPlaying = !value.isPlaying;
                    if (_wantPlaying && _foreground) {
                      unawaited(player.play());
                    } else {
                      unawaited(player.pause());
                    }
                  },
                  icon: Icon(value.isPlaying ? Icons.pause : Icons.play_arrow),
                ),
              ),
            if (!widget.live && player != null)
              Expanded(
                child: VideoProgressIndicator(player, allowScrubbing: true),
              ),
            if (widget.live) const Expanded(child: Text('실시간')),
            IconButton(
              tooltip: '영상 다시 연결',
              onPressed: _loading
                  ? null
                  : () {
                      _retried = false;
                      unawaited(_load());
                    },
              icon: const Icon(Icons.refresh),
            ),
          ],
        ),
      ],
    );
  }

  /// 주기 갱신·앱 수명주기·플레이어 리스너를 해제하고 네이티브 재생 자원을 반환한다.
  @override
  void dispose() {
    _loadGeneration++;
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _player?.removeListener(_playerChanged);
    unawaited(_disposePlayer(_player));
    super.dispose();
  }
}
