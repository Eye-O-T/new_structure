// 서버의 최신 객체 좌표를 표시한다. HLS 지연 때문에 영상 프레임과 정확히 일치하지 않는다.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';

/// 재생 영역 위에서 카메라의 최신 감지 결과를 주기적으로 받아 표시한다.
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
  // 기기 시계 보정과 무관한 경과 시간으로 마지막 성공 응답의 표시 수명을 잰다.
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

  /// 한 번에 하나의 조회만 실행하고 현재 카메라에 유효한 최신 응답만 적용한다.
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

  // 좌표 레이어가 영상의 터치 입력을 가로채지 않도록 포인터 처리를 통과시킨다.
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

/// 원본 프레임 픽셀의 감지 박스를 영상 영역 크기에 비례해 배치한다.
/// 프레임 크기와 박스 경계가 유효한 객체만 그리며 API 조회는 담당하지 않는다.
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
        // bbox [왼쪽, 위, 오른쪽, 아래]를 원본 픽셀에서 화면 좌표로 환산한다.
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
          // P는 카메라 추적 세션의 ID, G는 카메라 간 인물 ID다. 서버가 지정한 G만 표시한다.
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
