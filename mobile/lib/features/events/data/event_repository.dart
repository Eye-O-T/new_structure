import 'package:app/features/events/domain/event.dart';

abstract class EventRepository {
  Future<List<Event>> getEventsByDate(DateTime date);
}
