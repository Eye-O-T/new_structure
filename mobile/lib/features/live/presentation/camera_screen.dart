// 선택한 카메라의 영상·Edge 상태·지원 품질을 조회하고 관리자에게 품질 변경 기능을 제공한다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'protected_video.dart';

final cameraStatusProvider = FutureProvider.autoDispose
    .family<Map<String, dynamic>, String>(
      (ref, id) => ref
          .watch(apiClientProvider)
          .request('GET', '/api/v1/cameras/${Uri.encodeComponent(id)}/status'),
    );
final videoProfileProvider = FutureProvider.autoDispose
    .family<Map<String, dynamic>, String>(
      (ref, id) => ref
          .watch(apiClientProvider)
          .request(
            'GET',
            '/api/v1/cameras/${Uri.encodeComponent(id)}/video-profile',
          ),
    );

class CameraScreen extends ConsumerStatefulWidget {
  const CameraScreen({super.key, required this.cameraId});
  final String cameraId;
  @override
  ConsumerState<CameraScreen> createState() => _CameraScreenState();
}

class _CameraScreenState extends ConsumerState<CameraScreen> {
  bool busy = false;
  Future<void> changeProfile(String profile) async {
    setState(() => busy = true);
    try {
      await ref
          .read(apiClientProvider)
          .request(
            'PATCH',
            '/api/v1/cameras/${Uri.encodeComponent(widget.cameraId)}/video-profile',
            body: {'profile': profile},
          );
      // 요청값으로 화면을 미리 바꾸지 않고 서버가 확정한 품질과 상태를 다시 조회한다.
      ref.invalidate(videoProfileProvider(widget.cameraId));
      ref.invalidate(cameraStatusProvider(widget.cameraId));
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.toString())));
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final id = widget.cameraId;
    final status = ref.watch(cameraStatusProvider(id));
    final profile = ref.watch(videoProfileProvider(id));
    return Scaffold(
      appBar: AppBar(title: Text(id)),
      body: ListView(
        children: [
          ProtectedVideo(
            endpoint: '/api/v1/cameras/${Uri.encodeComponent(id)}/live',
            urlField: 'url',
            live: true,
            cameraId: id,
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: status.when(
              loading: () => const LinearProgressIndicator(),
              error: (e, _) => Text(e.toString()),
              data: (s) => Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('장치: ${s['online'] == true ? '온라인' : '오프라인'}'),
                  Text('카메라 입력: ${s['camera_input'] ?? '확인 불가'}'),
                  Text('중앙 연결: ${s['central_connection_status'] ?? '확인 불가'}'),
                  Text('배터리: ${s['battery_percent'] ?? '-'}%'),
                  Text('저장 공간 사용: ${s['storage_percent'] ?? '-'}%'),
                ],
              ),
            ),
          ),
          profile.when(
            loading: () => const SizedBox.shrink(),
            error: (e, _) => Padding(
              padding: const EdgeInsets.all(16),
              child: Text(e.toString()),
            ),
            data: (p) => Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('현재 영상 품질: ${p['current_profile'] ?? '-'}'),
                  if (ref.read(apiClientProvider).isAdmin)
                    Wrap(
                      spacing: 8,
                      children: [
                        for (final value
                            in (p['supported_profiles'] as List? ?? []))
                          OutlinedButton(
                            onPressed: busy
                                ? null
                                : () => changeProfile(value.toString()),
                            child: Text(value.toString().toUpperCase()),
                          ),
                      ],
                    ),
                  if (busy) const LinearProgressIndicator(),
                ],
              ),
            ),
          ),
          TextButton(
            onPressed: () {
              ref.invalidate(cameraStatusProvider(id));
              ref.invalidate(videoProfileProvider(id));
            },
            child: const Text('상태 새로고침'),
          ),
        ],
      ),
    );
  }
}
