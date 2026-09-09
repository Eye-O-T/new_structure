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
  String? _error;
  bool _loading = false;
  // 플레이어 오류의 자동 복구는 한 번만 하며 수동 재연결이나 endpoint 변경 때 초기화한다.
  bool _retried = false;
  bool _showObjects = true;
  bool _foreground = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    unawaited(_load());
    _timer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => unawaited(_renew()),
    );
  }

  @override
  void didUpdateWidget(ProtectedVideo oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.endpoint != widget.endpoint) {
      _retried = false;
      unawaited(_load());
    }
  }

  /// 주기적으로 access 토큰을 확인하고 헤더가 바뀌었을 때만 플레이어를 재생성한다.
  Future<void> _renew() async {
    if (!mounted || _loading) return;
    try {
      final token = await ref.read(apiClientProvider).accessToken();
      if (token != _token) await _load();
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    }
  }

  /// 재생 주소와 토큰을 새로 받아 플레이어를 교체하며 동시에 두 초기화를 실행하지 않는다.
  /// refresh는 플레이어 오류 복구 시 세션 갱신을 먼저 수행하도록 한다.
  Future<void> _load({bool refresh = false}) async {
    if (_loading) return;
    _loading = true;
    if (mounted) setState(() => _error = null);
    VideoPlayerController? next;
    final old = _player;
    // 녹화는 보던 위치를 이어 가고 실시간 영상은 새 연결의 현재 지점에서 시작한다.
    final position = widget.live
        ? Duration.zero
        : old?.value.position ?? Duration.zero;
    try {
      final api = ref.read(apiClientProvider);
      if (refresh) await api.refreshSession();
      final descriptor = await api.request('GET', widget.endpoint);
      final uri = api.mediaUri(descriptor[widget.urlField] as String);
      final token = await api.accessToken();
      next = VideoPlayerController.networkUrl(
        uri,
        httpHeaders: {'Authorization': 'Bearer $token'},
      );
      await next.initialize().timeout(const Duration(seconds: 25));
      // 초기화 대기 중 화면이 닫혔다면 새 플레이어를 연결하지 않고 즉시 정리한다.
      if (!mounted) {
        await next.dispose();
        return;
      }
      if (position > Duration.zero) await next.seekTo(position);
      await next.play();
      // 새 플레이어가 준비된 뒤 이전 리스너와 자원을 정리해 활성 플레이어를 교체한다.
      _token = token;
      _player = next;
      next.addListener(_playerChanged);
      old?.removeListener(_playerChanged);
      await old?.dispose();
    } catch (_) {
      if (next != null && next != _player) await next.dispose();
      if (mounted) _error = '영상을 재생하지 못했습니다. 카메라와 서버 연결을 확인하세요.';
    } finally {
      _loading = false;
      if (mounted) setState(() {});
    }
  }

  /// 네이티브 플레이어의 오류를 감지해 토큰 갱신을 포함한 복구를 한 번 시도한다.
  void _playerChanged() {
    if (!mounted) return;
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
                  onPressed: () =>
                      value.isPlaying ? player.pause() : player.play(),
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
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _player?.removeListener(_playerChanged);
    unawaited(_player?.dispose());
    super.dispose();
  }
}
