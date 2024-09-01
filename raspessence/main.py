from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from enum import Enum
import os
from typing import Self

import uvloop
from attr import define

from . import ConfigError, webserver
from .dbus import DbusHandler, SpotifyCommand, SpotifyEvent
from .lintronic import BeoCommand, LinTronicConnection


logger = logging.getLogger(__name__)

FIVE_MINUTES = timedelta(minutes=5)
FIFTEEN_MINUTES = timedelta(minutes=15)


class PlaybackStatus(Enum):
    PLAYING = "Playing"
    PAUSED = "Paused"
    STOPPED = "Stopped"
    UNKNOWN = "Unknown"


@define
class State:
    last_known_volume: float = 0.5
    last_known_playback_status: PlaybackStatus = PlaybackStatus.UNKNOWN


class MainHandler:
    def __init__(self, ltc: LinTronicConnection, dbus_handler: DbusHandler) -> None:
        self.ltc = ltc
        self.dbus_handler = dbus_handler
        self.shutdown_timer_task: asyncio.Task | None = None
        self.state = State()

    @classmethod
    async def create(cls, lintronic_url: str, auth_secret: str) -> Self:
        ltc = await LinTronicConnection.create(lintronic_url)
        dbus_handler = await DbusHandler.create()
        self = cls(ltc, dbus_handler)
        dbus_handler.register_spotify_callback(
            SpotifyEvent.PLAYBACK_STATUS, self.playback_handler
        )
        dbus_handler.register_spotify_callback(SpotifyEvent.VOLUME, self.volume_handler)
        await webserver.start_server(self.power_off_handler, auth_secret)
        return self

    async def wait(self) -> None:
        await asyncio.Event().wait()

    async def volume_handler(self, volume: float) -> None:
        if volume > self.state.last_known_volume:
            logger.debug(
                "Volume was %s and is now %s: turning volume up",
                self.state.last_known_volume,
                volume,
            )
            await self.ltc.write_message_to_lintronic(BeoCommand.VOLUME_UP)
        else:
            logger.debug(
                "Volume was %s and is now %s: turning volume down",
                self.state.last_known_volume,
                volume,
            )
            await self.ltc.write_message_to_lintronic(BeoCommand.VOLUME_DOWN)
        self.state.last_known_volume = volume

    async def playback_handler(self, status: str) -> None:
        if status == self.state.last_known_playback_status.value:
            return

        try:
            playback_status = PlaybackStatus(status)
        except ValueError:
            logger.warning("Unknown playback status %s", status)
            self.state.last_known_playback_status = PlaybackStatus.UNKNOWN
            return

        logger.debug("Playback status changed to '%s'", playback_status)
        match playback_status:
            case PlaybackStatus.PLAYING:
                logger.debug("Sending AUDIO AUX command and disabling shutdown timer")
                await self.ltc.write_message_to_lintronic(BeoCommand.AUDIO_AUX)
                if self.shutdown_timer_task is not None:
                    self.shutdown_timer_task.cancel()
            case PlaybackStatus.PAUSED:
                # If we went from Stopped -> Paused, that means that we've been
                # choosen as a SpotifyConnect target, and music will most likely start
                # playing soon. Wake the BeoMaster, so it's ready when the music comes.
                if self.state.last_known_playback_status == PlaybackStatus.STOPPED:
                    logger.debug(
                        "Went from stopped -> paused. Sending AUDIO AUX command"
                    )
                    await self.ltc.write_message_to_lintronic(BeoCommand.AUDIO_AUX)
                logger.debug("Starting shutdown timer for 15 minutes")
                self.start_shutdown_timer(FIFTEEN_MINUTES)
            case PlaybackStatus.STOPPED:
                logger.debug("Starting shutdown timer for 5 minutes")
                self.start_shutdown_timer(FIVE_MINUTES)

        self.state.last_known_playback_status = playback_status

    async def power_off_handler(self) -> None:
        await self.ltc.write_message_to_lintronic(
            BeoCommand.VOLUME_DOWN, repeat_count=10
        )

        await asyncio.sleep(1)

        await self.dbus_handler.send_spotify_command(SpotifyCommand.PAUSE)
        await self.ltc.write_message_to_lintronic(BeoCommand.AUDIO_POWER_OFF)
        self.state.last_known_playback_status = PlaybackStatus.PAUSED
        if self.shutdown_timer_task is not None:
            self.shutdown_timer_task.cancel()

    def start_shutdown_timer(self, timeout: timedelta) -> None:
        if self.shutdown_timer_task is not None:
            self.shutdown_timer_task.cancel()
        self.shutdown_timer_task = asyncio.create_task(self.shutdown_timer(timeout))

    async def shutdown_timer(self, timeout: timedelta) -> None:
        logger.debug("Shutdown timer initiated")
        await asyncio.sleep(timeout.total_seconds())
        logger.debug("Shutdown timer activated - shutting down")
        if self.state.last_known_playback_status not in (
            PlaybackStatus.PAUSED,
            PlaybackStatus.STOPPED,
        ):
            logger.warning(
                "Shutdown timer woke up, but playback was not paused/stopped. Was: %s",
                self.state.last_known_playback_status,
            )
            return
        await self.ltc.write_message_to_lintronic(BeoCommand.AUDIO_POWER_OFF)


async def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.DEBUG,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not (auth_secret := os.environ.get("RASPESSENCE_AUTH_SECRET")):
        raise ConfigError("Missing 'RASPESSENCE_AUTH_SECRET' env variable")

    main = await MainHandler.create("/dev/ttyUSB0", auth_secret)
    logger.info("up and running")
    await main.wait()


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        runner.run(main())
