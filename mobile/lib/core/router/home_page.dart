// 실시간·히스토리·설정 화면에 공통 하단 메뉴를 제공하는 화면 틀이다.
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

/// 현재 경로를 선택된 탭으로 표시하고 라우터의 하위 화면을 공통 틀 안에 배치한다.
class HomePage extends StatelessWidget {
  const HomePage({super.key, required this.child});

  final Widget child;

  // NavigationBar의 destinations 순서와 경로 순서가 같아야 선택 인덱스가 일치한다.
  static const List<String> _routes = ['/live', '/history', '/settings'];

  @override
  Widget build(BuildContext context) {
    final location = GoRouterState.of(context).uri.path;

    int currentIndex = _routes.indexOf(location);
    if (currentIndex == -1) currentIndex = 0;

    return Scaffold(
      body: child,

      bottomNavigationBar: NavigationBar(
        selectedIndex: currentIndex,

        onDestinationSelected: (index) {
          context.go(_routes[index]);
        },

        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.videocam_outlined),
            selectedIcon: Icon(Icons.videocam),
            label: '실시간',
          ),
          NavigationDestination(
            icon: Icon(Icons.history_outlined),
            selectedIcon: Icon(Icons.history),
            label: '히스토리',
          ),
          NavigationDestination(
            icon: Icon(Icons.settings_outlined),
            selectedIcon: Icon(Icons.settings),
            label: '설정',
          ),
        ],
      ),
    );
  }
}
