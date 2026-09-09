// 서버가 현재 사용자에게 허용한 카메라 목록을 표시하고 개별 실시간 화면으로 연결한다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:app/features/events/presentation/event_history_view_model.dart';

/// 접근 가능한 카메라의 로딩·오류·빈 목록 상태와 개별 화면 진입을 제공한다.
class LiveScreen extends ConsumerWidget {
  const LiveScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cameras = ref.watch(camerasProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('AI CCTV'),
        actions: [
          IconButton(
            tooltip: '새로고침',
            onPressed: () => ref.invalidate(camerasProvider),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: cameras.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(error.toString()),
              TextButton(
                onPressed: () => ref.invalidate(camerasProvider),
                child: const Text('다시 시도'),
              ),
            ],
          ),
        ),
        data: (rows) => rows.isEmpty
            ? const Center(child: Text('접근 가능한 카메라가 없습니다.'))
            : RefreshIndicator(
                // 목록 무효화만으로 끝내지 않고 새 응답까지 기다려 새로고침 완료를 표시한다.
                onRefresh: () async {
                  ref.invalidate(camerasProvider);
                  await ref.read(camerasProvider.future);
                },
                child: ListView.builder(
                  physics: const AlwaysScrollableScrollPhysics(),
                  itemCount: rows.length,
                  itemBuilder: (_, index) {
                    final camera = rows[index];
                    final id = camera['camera_id'].toString();
                    return Card(
                      margin: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 8,
                      ),
                      child: ListTile(
                        leading: const Icon(Icons.videocam_outlined),
                        title: Text((camera['name'] ?? id).toString()),
                        subtitle: Text(id),
                        trailing: const Icon(Icons.chevron_right),
                        onTap: () =>
                            context.push('/cameras/${Uri.encodeComponent(id)}'),
                      ),
                    );
                  },
                ),
              ),
      ),
    );
  }
}
