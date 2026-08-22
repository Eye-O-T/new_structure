# discord_bot.py
#
# 역할:
# - discord.py를 사용해 Discord 봇 계정으로 특정 채널에 메시지를 보냅니다.
# - chat_bot.py의 큐 worker가 이 모듈의 send_message() 함수를 호출합니다.
# - Discord Client는 한 번 로그인한 뒤 재사용하므로, 메시지마다 새로 로그인하지 않습니다.

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import threading

import discord


# Discord 메시지 길이 제한에 걸리지 않도록 여유 있게 1900자 단위로 나눕니다.
_MAX_MESSAGE_LENGTH = 1900

# 모듈 전역 sender를 보호하는 lock입니다.
# 여러 thread에서 send_message()를 동시에 호출해도 sender는 하나만 생성됩니다.
_default_sender_lock = threading.Lock()

# chat_bot.py에서 사용하는 기본 Discord sender입니다.
# 첫 메시지를 보낼 때 환경변수를 읽어 lazy-create합니다.
_default_sender: DiscordBotSender | None = None


def _read_secret(key: str) -> str:
    """Read a secret from the process environment, then a local ignored file."""
    environment_value = os.getenv(key, "").strip()
    if environment_value:
        return environment_value

    proj_env_path = Path(__file__).resolve().parents[2] / ".proj_env"

    try:
        for line in proj_env_path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue

            if "=" in value:
                current_key, current_value = value.split("=", 1)
                if current_key.strip() == key:
                    return current_value.strip()
                continue

            if key == "DISCORD_BOT_TOKEN":
                return value
    except FileNotFoundError:
        pass

    return ""


class DiscordBotSender:
    """Discord 봇 로그인과 메시지 전송을 담당하는 클래스입니다."""

    def __init__(
        self, token: str | None = None, channel_id: int | str | None = None
    ) -> None:
        """Discord 전송 객체를 초기화합니다.

        Args:
            token: Discord Bot Token입니다. 없으면 환경변수 또는 로컬 .proj_env를 사용합니다.
            channel_id: 메시지를 보낼 Discord 채널 ID입니다.
        """
        self.token = (token or _read_secret("DISCORD_BOT_TOKEN")).strip()

        # Discord 개발자 모드에서 채널을 우클릭해 "ID 복사"로 얻은 값을 넣으면 됩니다.
        raw_channel_id = (
            channel_id if channel_id is not None else _read_secret("DISCORD_CHANNEL_ID")
        )

        if not self.token:
            raise RuntimeError(
                "Discord 토큰이 환경변수 또는 .proj_env에 설정되지 않았습니다."
            )

        if raw_channel_id is None or str(raw_channel_id).strip() == "":
            raise RuntimeError(
                "Discord 채널 ID가 환경변수 또는 .proj_env에 설정되지 않았습니다."
            )

        try:
            # discord.py의 get_channel/fetch_channel은 int snowflake ID를 사용합니다.
            self.channel_id = int(str(raw_channel_id).strip())
        except ValueError as exc:
            raise RuntimeError(
                "DISCORD_CHANNEL_ID는 숫자 형태의 Discord 채널 ID여야 합니다."
            ) from exc

        # discord.py Client가 실행될 asyncio event loop입니다.
        # VLMWorker는 일반 thread 기반 코드이므로, Discord용 event loop를 별도 thread에서 실행합니다.
        self.loop: asyncio.AbstractEventLoop | None = None

        # 실제 Discord 연결을 담당하는 client입니다.
        self.client: discord.Client | None = None

        # Discord event loop를 실행하는 thread입니다.
        self.thread: threading.Thread | None = None

        # on_ready 이벤트가 발생해 메시지를 보낼 준비가 되었는지 알려주는 event입니다.
        self.ready_event = threading.Event()

        # 로그인 실패 등 시작 과정에서 발생한 예외를 send_message() 호출자에게 전달하기 위한 저장소입니다.
        self.startup_error: BaseException | None = None

        # start() 중복 실행을 막기 위한 lock입니다.
        self.start_lock = threading.Lock()

    def start(self, timeout: float = 30.0) -> None:
        """Discord client를 시작하고 준비 완료까지 기다립니다.

        Args:
            timeout: 봇 로그인이 완료될 때까지 기다릴 최대 초 단위 시간입니다.
        """
        with self.start_lock:
            # 이미 event loop thread가 살아 있으면 새로 시작하지 않습니다.
            if self.thread is None or not self.thread.is_alive():
                self.ready_event.clear()
                self.startup_error = None

                self.thread = threading.Thread(
                    target=self._run_event_loop,
                    name="DiscordBotEventLoop",
                    daemon=True,
                )
                self.thread.start()

        # on_ready 또는 시작 실패가 발생할 때까지 기다립니다.
        if not self.ready_event.wait(timeout=timeout):
            raise TimeoutError("Discord 봇 로그인 대기 시간이 초과되었습니다.")

        # event loop thread에서 로그인 실패가 발생했다면 호출자에게 전달합니다.
        if self.startup_error is not None:
            raise RuntimeError(
                f"Discord 봇 시작 실패: {self.startup_error}"
            ) from self.startup_error

    def send_message(self, content: str, timeout: float = 30.0) -> None:
        """Discord 채널로 메시지를 전송합니다.

        Args:
            content: Discord로 보낼 문자열입니다.
            timeout: 전송 완료까지 기다릴 최대 초 단위 시간입니다.
        """
        # Discord client가 아직 시작되지 않았다면 먼저 시작합니다.
        self.start()

        if self.loop is None or self.client is None:
            raise RuntimeError("Discord client가 초기화되지 않았습니다.")

        if not self.loop.is_running():
            raise RuntimeError("Discord event loop가 실행 중이 아닙니다.")

        # 현재 함수는 일반 thread에서 호출됩니다.
        # asyncio.run_coroutine_threadsafe()로 Discord event loop thread에 coroutine 실행을 요청합니다.
        future = asyncio.run_coroutine_threadsafe(
            self._send_message_async(content),
            self.loop,
        )

        # chat_bot.py의 worker thread 안에서만 기다리므로 VLMWorker thread는 막히지 않습니다.
        future.result(timeout=timeout)

    async def _send_message_async(self, content: str) -> None:
        """Discord event loop 안에서 실제 메시지를 전송합니다."""
        if self.client is None:
            raise RuntimeError("Discord client가 없습니다.")

        # on_ready 이후에도 재연결 중일 수 있으므로 안전하게 준비 완료를 한 번 더 기다립니다.
        await self.client.wait_until_ready()

        # cache에서 채널을 먼저 찾습니다.
        # cache에 없으면 REST API로 채널 정보를 가져옵니다.
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(self.channel_id)

        # 텍스트 채널, 스레드, DM 등 메시지 전송 가능 객체는 send() 메서드를 가집니다.
        if not hasattr(channel, "send"):
            raise RuntimeError(
                f"채널 {self.channel_id}은 메시지를 보낼 수 있는 채널이 아닙니다."
            )

        # Discord 메시지 길이 제한을 넘지 않도록 여러 메시지로 분할해 순서대로 보냅니다.
        for chunk in _split_message(content):
            await channel.send(chunk)

    def close(self, timeout: float = 10.0) -> None:
        """Discord client와 event loop thread를 종료합니다."""
        loop = self.loop
        client = self.client
        thread = self.thread

        # event loop가 살아 있고 client가 닫히지 않았다면 close coroutine을 event loop에 예약합니다.
        if (
            loop is not None
            and loop.is_running()
            and client is not None
            and not client.is_closed()
        ):
            future = asyncio.run_coroutine_threadsafe(client.close(), loop)

            # 종료 중 예외가 발생해도 프로세스 종료를 막지 않도록 출력만 합니다.
            with contextlib.suppress(Exception):
                future.result(timeout=timeout)

        # event loop thread가 종료될 시간을 줍니다.
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

        # thread가 실제로 종료된 경우, 다음 send_message() 호출 때 새 client를 만들 수 있게 참조를 비웁니다.
        if thread is None or not thread.is_alive():
            self.thread = None
            self.loop = None
            self.client = None
            self.ready_event.clear()

    def _run_event_loop(self) -> None:
        """별도 thread에서 Discord client용 asyncio event loop를 실행합니다."""
        # 이 thread 전용 event loop를 만듭니다.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        self.loop = loop

        # 메시지를 보내기만 하므로 privileged intent는 켜지 않습니다.
        # 기본 intent만 사용해 봇 로그인과 채널 메시지 전송을 수행합니다.
        intents = discord.Intents.default()

        # discord.py Client 인스턴스를 생성합니다.
        client = discord.Client(intents=intents)
        self.client = client

        @client.event
        async def on_ready() -> None:
            """Discord 로그인과 초기 동기화가 완료되면 호출됩니다."""
            print(f"Discord 봇 로그인 완료: {client.user}")
            self.ready_event.set()

        try:
            # client.start()는 logout/close 전까지 실행되는 coroutine입니다.
            # token이 잘못되었거나 네트워크 문제가 있으면 예외가 발생합니다.
            loop.run_until_complete(client.start(self.token))

        except Exception as exc:
            # start()를 기다리는 thread가 무한 대기하지 않도록 실패 상태를 기록하고 event를 깨웁니다.
            self.startup_error = exc
            self.ready_event.set()
            print(f"Discord 봇 실행 실패: {exc}")

        finally:
            # 남아 있는 asyncio task를 정리합니다.
            pending_tasks = asyncio.all_tasks(loop)
            for task in pending_tasks:
                task.cancel()

            if pending_tasks:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(
                        asyncio.gather(*pending_tasks, return_exceptions=True)
                    )

            # event loop 자원을 닫습니다.
            loop.close()


def send_message(content: str) -> None:
    """기본 Discord sender를 사용해 메시지를 보냅니다.

    chat_bot.py에서는 이 함수만 호출하면 됩니다.
    """
    global _default_sender

    with _default_sender_lock:
        # sender는 첫 메시지 전송 시점에 생성합니다.
        # 이렇게 하면 .proj_env가 없는 개발 환경에서도 import 자체는 실패하지 않습니다.
        if _default_sender is None:
            _default_sender = DiscordBotSender()

        sender = _default_sender

    sender.send_message(content)


def close() -> None:
    """기본 Discord sender를 종료합니다."""
    global _default_sender

    with _default_sender_lock:
        sender = _default_sender
        _default_sender = None

    if sender is not None:
        sender.close()


def _split_message(content: str) -> list[str]:
    """Discord 전송용으로 긴 문자열을 여러 조각으로 나눕니다."""
    text = str(content).strip()

    # 빈 문자열은 Discord에서 전송할 수 없으므로 대체 문구를 보냅니다.
    if not text:
        return ["빈 메시지입니다."]

    chunks: list[str] = []

    # 단순한 길이 기준 분할입니다.
    # VLM 결과는 일반 텍스트이므로 복잡한 markdown 보존 로직은 넣지 않습니다.
    while text:
        chunks.append(text[:_MAX_MESSAGE_LENGTH])
        text = text[_MAX_MESSAGE_LENGTH:]

    return chunks
