// 현지 날짜 기준으로 한 주를 표시하고 선택 상태만 변경한다. UTC 변환은 조회 저장소에서 수행한다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'package:app/features/events/presentation/event_history_view_model.dart';

class HistoryDateSelector extends ConsumerWidget {
  const HistoryDateSelector({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final selectedDate = ref.watch(selectedDateProvider);

    // 선택 날짜가 포함된 주의 일요일
    final startOfWeek = selectedDate.subtract(
      Duration(days: selectedDate.weekday % 7),
    );

    final weekDates = List.generate(
      7,
      (index) => startOfWeek.add(Duration(days: index)),
    );

    return Column(
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            IconButton(
              onPressed: () {
                ref
                    .read(selectedDateProvider.notifier)
                    .selectDate(selectedDate.subtract(const Duration(days: 7)));
              },
              icon: const Icon(Icons.chevron_left),
            ),

            Text(
              _weekTitle(selectedDate),
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold),
            ),
            IconButton(
              onPressed: () {
                final newDate = selectedDate.add(const Duration(days: 7));

                ref.read(selectedDateProvider.notifier).selectDate(newDate);
              },
              icon: const Icon(Icons.chevron_right),
            ),
          ],
        ),

        const SizedBox(height: 16),

        Row(
          mainAxisAlignment: MainAxisAlignment.spaceAround,
          children: weekDates.map((date) {
            final isSelected = _isSameDay(date, selectedDate);

            return GestureDetector(
              onTap: () {
                ref.read(selectedDateProvider.notifier).selectDate(date);
              },
              child: Column(
                children: [
                  Text(
                    '${date.day}일',
                    style: TextStyle(
                      fontWeight: isSelected
                          ? FontWeight.bold
                          : FontWeight.normal,
                    ),
                  ),

                  const SizedBox(height: 4),

                  Text(
                    _weekdayName(date.weekday),
                    style: TextStyle(
                      fontWeight: isSelected
                          ? FontWeight.bold
                          : FontWeight.normal,
                    ),
                  ),

                  const SizedBox(height: 4),

                  Container(
                    width: 30,
                    height: 3,
                    color: isSelected ? Colors.blue : Colors.transparent,
                  ),
                ],
              ),
            );
          }).toList(),
        ),
      ],
    );
  }

  bool _isSameDay(DateTime a, DateTime b) {
    return a.year == b.year && a.month == b.month && a.day == b.day;
  }

  String _weekdayName(int weekday) {
    switch (weekday) {
      case DateTime.monday:
        return 'MON';
      case DateTime.tuesday:
        return 'TUE';
      case DateTime.wednesday:
        return 'WED';
      case DateTime.thursday:
        return 'THU';
      case DateTime.friday:
        return 'FRI';
      case DateTime.saturday:
        return 'SAT';
      case DateTime.sunday:
        return 'SUN';
      default:
        return '';
    }
  }

  String _weekTitle(DateTime date) {
    final weekOfMonth = ((date.day - 1) ~/ 7) + 1;

    return '${date.year}년 ${date.month}월 $weekOfMonth주차';
  }
}
