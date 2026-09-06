import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import '../domain/event.dart';

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
