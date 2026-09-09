// 이벤트 요약과 현지 발생 시각을 표시한다. 탭하면 ID를 넘겨 상세 화면에서 최신 정보를 조회한다.
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import '../domain/event.dart';

/// 한 이벤트의 카메라·현지 시각 요약과 상세 화면 진입을 제공한다.
class EventCard extends StatelessWidget {
  const EventCard({super.key, required this.event});
  final Event event;

  @override
  Widget build(BuildContext context) => Card(
    margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
    child: ListTile(
      leading: const Icon(Icons.notifications_active_outlined),
      title: Text(event.title),
      subtitle: Text(
        '${event.cameraId}\n${event.occurredAt.toLocal().toString().split('.').first}',
      ),
      isThreeLine: true,
      trailing: const Icon(Icons.chevron_right),
      onTap: () => context.push('/events/${Uri.encodeComponent(event.id)}'),
    ),
  );
}
