// 서버가 제공하는 최신 객체 위치를 영상 위에 그린다. 모바일에서 탐지나 재식별을 수행하지 않는다.
// HLS 영상과 좌표 API는 도착 시간이 다르므로 같은 프레임에 정확히 맞춘 박스는 아니다.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';

/// Latest detector positions. HLS has a separate playback delay; this is not
/// a frame-synchronized video annotation stream.
class ObjectOverlay extends ConsumerStatefulWidget {
  const ObjectOverlay({super.key, required this.cameraId});
  final String cameraId;

  @override
  ConsumerState<ObjectOverlay> createState() => _ObjectOverlayState();
}

class _ObjectOverlayState extends ConsumerState<ObjectOverlay> {
  Timer? _timer;
  bool _fetching = false;
  Map<String, dynamic>? _frame;
  final Stopwatch _age = Stopwatch();

  @override
  void initState() {
    super.initState();
    unawaited(_poll());
    _timer = Timer.periodic(const Duration(milliseconds: 500), (_) {
      // 네트워크가 멈췄을 때 오래된 위치가 현재 위치처럼 남지 않도록 3초 뒤 지운다.
      if (_age.elapsed > const Duration(seconds: 3) && _frame != null) {
        setState(() => _frame = null);
      }
      unawaited(_poll());
    });
  }

  @override
  void didUpdateWidget(ObjectOverlay oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.cameraId != widget.cameraId) _frame = null;
  }

  Future<void> _poll() async {
    if (_fetching) return;
    _fetching = true;
    final camera = widget.cameraId;
    try {
      final result = await ref
          .read(apiClientProvider)
          .request(
            'GET',
            '/api/v1/cameras/${Uri.encodeComponent(camera)}/objects',
          );
      if (!mounted || camera != widget.cameraId) return;
      // 서버가 stale로 표시한 좌표는 버리고, 카메라 전환 전 요청의 늦은 응답도 적용하지 않는다.
      _age
        ..reset()
        ..start();
      setState(() => _frame = result['stale'] == false ? result : null);
    } catch (_) {
      if (mounted) setState(() => _frame = null);
    } finally {
      _fetching = false;
    }
  }

  @override
  Widget build(BuildContext context) =>
      IgnorePointer(child: ObjectBoxes(frame: _frame));

  @override
  void dispose() {
    _timer?.cancel();
    _age.stop();
    super.dispose();
  }
}

class ObjectBoxes extends StatelessWidget {
  const ObjectBoxes({super.key, required this.frame});
  final Map<String, dynamic>? frame;

  @override
  Widget build(BuildContext context) {
    final width = (frame?['frame_width'] as num?)?.toDouble() ?? 0;
    final height = (frame?['frame_height'] as num?)?.toDouble() ?? 0;
    if (width <= 0 || height <= 0) return const SizedBox.shrink();
    return LayoutBuilder(
      builder: (context, bounds) {
        // 품질 변경 도중 영상과 좌표의 가로세로 비율이 다르면 엉뚱한 위치에 그리지 않고 숨긴다.
        if ((bounds.maxWidth / bounds.maxHeight - width / height).abs() >
            0.05) {
          return const SizedBox.shrink();
        }
        final children = <Widget>[];
        // bbox는 원본 프레임의 [왼쪽, 위, 오른쪽, 아래] 픽셀 좌표다.
        // 원본 크기로 나눈 비율을 현재 화면 크기에 곱해 해상도가 다른 단말에서도 위치를 맞춘다.
        for (final object in (frame?['objects'] as List? ?? [])) {
          final box = object['bbox'] as List;
          final left = (box[0] as num).toDouble();
          final top = (box[1] as num).toDouble();
          final right = (box[2] as num).toDouble();
          final bottom = (box[3] as num).toDouble();
          if (left < 0 ||
              top < 0 ||
              right > width ||
              bottom > height ||
              right <= left ||
              bottom <= top) {
            continue;
          }
          final global = object['global_person_id'];
          // P는 카메라 추적 세션 안의 person_id, G는 여러 카메라를 연결하는 global_person_id다.
          // 재식별은 서버의 교체 가능한 블랙박스이므로 아직 없는 G 값을 임의로 만들지 않는다.
          children.add(
            Positioned(
              left: left / width * bounds.maxWidth,
              top: top / height * bounds.maxHeight,
              width: (right - left) / width * bounds.maxWidth,
              height: (bottom - top) / height * bounds.maxHeight,
              child: DecoratedBox(
                decoration: BoxDecoration(
                  border: Border.all(color: Colors.lightGreenAccent, width: 2),
                ),
                child: Align(
                  alignment: Alignment.topLeft,
                  child: ColoredBox(
                    color: Colors.black87,
                    child: Text(
                      'P:${object['person_id']}${global == null ? '' : ' G:$global'}',
                      maxLines: 1,
                      overflow: TextOverflow.clip,
                      style: const TextStyle(
                        color: Colors.lightGreenAccent,
                        fontSize: 12,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          );
        }
        return ClipRect(child: Stack(children: children));
      },
    );
  }
}
