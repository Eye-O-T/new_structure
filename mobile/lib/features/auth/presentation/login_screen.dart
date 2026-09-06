import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/config/api_config.dart';
import 'package:app/core/network/providers.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});
  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final server = TextEditingController(text: ApiConfig.initialOrigin);
  final username = TextEditingController();
  final password = TextEditingController();
  bool busy = false;
  String? error;

  @override
  void initState() {
    super.initState();
    final previous = ref.read(apiClientProvider).origin;
    if (previous != null) server.text = previous.toString();
  }

  Future<void> submit() async {
    if (busy) return;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await ref
          .read(apiClientProvider)
          .login(server.text, username.text, password.text);
      password.clear();
    } catch (failure) {
      if (mounted) {
        setState(
          () => error = failure is FormatException
              ? failure.message
              : failure.toString(),
        );
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('AI CCTV 로그인')),
    body: Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: AutofillGroup(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextField(
                  controller: server,
                  enabled: !busy,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: '중앙 서버 주소',
                    hintText: 'https://cctv.example.com',
                  ),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: username,
                  enabled: !busy,
                  autofillHints: const [AutofillHints.username],
                  autocorrect: false,
                  decoration: const InputDecoration(labelText: '계정'),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: password,
                  enabled: !busy,
                  obscureText: true,
                  autofillHints: const [AutofillHints.password],
                  decoration: const InputDecoration(labelText: '비밀번호'),
                  onSubmitted: (_) => submit(),
                ),
                const SizedBox(height: 24),
                if (error != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 16),
                    child: Text(error!),
                  ),
                FilledButton(
                  onPressed: busy ? null : submit,
                  child: Text(busy ? '로그인 중…' : '로그인'),
                ),
              ],
            ),
          ),
        ),
      ),
    ),
  );

  @override
  void dispose() {
    server.dispose();
    username.dispose();
    password.dispose();
    super.dispose();
  }
}
