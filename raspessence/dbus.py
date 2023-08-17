import logging
from enum import Enum
from typing import Awaitable, Callable, Self

from dbus_fast.aio.message_bus import MessageBus
from dbus_fast.proxy_object import BaseProxyInterface

logger = logging.getLogger(__name__)


class SpotifyCommand(Enum):
    PAUSE = "pause"


class SpotifyEvent(Enum):
    VOLUME = "Volume"
    PLAYBACK_STATUS = "PlaybackStatus"


class SpotifydHandler:
    def __init__(
        self,
        bus_name: str,
        player: BaseProxyInterface,
        callbacks: dict[str, Callable[..., Awaitable]],
    ) -> None:
        self.bus_name = bus_name
        self.player = player
        self.callbacks = callbacks

    @classmethod
    async def create(
        cls,
        message_bus: MessageBus,
        bus_name: str,
        callbacks: dict[str, Callable[..., Awaitable]],
    ) -> Self:
        introspection = await message_bus.introspect(
            bus_name, "/org/mpris/MediaPlayer2"
        )
        proxy = message_bus.get_proxy_object(
            bus_name, "/org/mpris/MediaPlayer2", introspection
        )

        player = proxy.get_interface("org.mpris.MediaPlayer2.Player")
        properties = proxy.get_interface("org.freedesktop.DBus.Properties")

        self = cls(bus_name, player, callbacks)
        properties.on_properties_changed(self._on_properties_changed)  # type: ignore
        logger.info("Connected to spotifyd at %s", bus_name)
        return self

    async def _on_properties_changed(
        self, interface_name, changed_properties, invalidated_properties
    ):
        for changed, variant in changed_properties.items():
            if callback := self.callbacks.get(changed):
                await callback(variant.value)

    async def send_command(self, spotify_command: SpotifyCommand) -> None:
        if spotify_command == SpotifyCommand.PAUSE:
            await self.player.call_pause()  # type: ignore
        else:
            raise NotImplementedError


class DbusHandler:
    def __init__(
        self,
        message_bus: MessageBus,
        dbus: BaseProxyInterface,
        spotify_callbacks: dict[str, Callable[..., Awaitable]],
        spotify_handler: SpotifydHandler | None,
    ) -> None:
        self.message_bus = message_bus
        self.dbus = dbus
        self.spotify_callbacks = spotify_callbacks
        self.spotify_handler = spotify_handler

    @classmethod
    async def create(cls) -> Self:
        message_bus = await MessageBus().connect()

        # org.freedesktop.DBus? org.freedesktop.DBus!
        introspection = await message_bus.introspect(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
        )

        proxy = message_bus.get_proxy_object(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            introspection,
        )
        dbus = proxy.get_interface("org.freedesktop.DBus")

        spotify_callbacks: dict[str, Callable[..., Awaitable]] = {}

        for name in await dbus.call_list_names():  # type: ignore
            if name and name.startswith("org.mpris.MediaPlayer2.spotifyd"):
                logger.debug("Connecting to already running spotifyd at %s", name)
                spotifyd_handler = await SpotifydHandler.create(
                    message_bus, name, spotify_callbacks
                )
                self = cls(message_bus, dbus, spotify_callbacks, spotifyd_handler)
                break
        else:
            logger.debug("No running spotifyd instance detected")
            self = cls(message_bus, dbus, spotify_callbacks, spotify_handler=None)

        dbus.on_name_owner_changed(self._on_name_owner_changed)  # type: ignore
        return self

    async def _on_name_owner_changed(self, bus_name: str, _, __) -> None:
        if not bus_name or not bus_name.startswith("org.mpris.MediaPlayer2.spotifyd"):
            return

        has_owner = await self.dbus.call_name_has_owner(bus_name)  # type: ignore
        if (
            self.spotify_handler is not None
            and self.spotify_handler.bus_name == bus_name
            and not has_owner
        ):
            logger.debug("Lost contact with spotifyd at %s", bus_name)
            self.spotify_handler = None

        elif self.spotify_handler is None and has_owner:
            logger.debug("New spotifyd has appeared at %s", bus_name)
            self.spotify_handler = await SpotifydHandler.create(
                self.message_bus, bus_name, self.spotify_callbacks
            )

    def register_spotify_callback(
        self, spotify_event: SpotifyEvent, handler: Callable[..., Awaitable]
    ) -> None:
        self.spotify_callbacks[spotify_event.value] = handler

    async def send_spotify_command(self, spotify_command: SpotifyCommand) -> None:
        if self.spotify_handler is None:
            logger.warning(
                "Could not send spotify command, as has no connection with spotifyd"
            )
            return
        await self.spotify_handler.send_command(spotify_command)
