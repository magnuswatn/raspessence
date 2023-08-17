import asyncio
import logging
from enum import Enum
from typing import Self

import serial_asyncio

LINTRONIC_ADDRESS = b"01"
OUR_ADDRESS = b"00"

START_OF_TRANSMISSION = b"<"
END_OF_TRANSMISSION = b">"

MAGIC_SUFFIX = b"024"


logger = logging.getLogger(__name__)


class BeoCommand(Enum):
    VOLUME_DOWN = b"040255010010701001100000000000000"
    VOLUME_UP = b"040255010010701000096000000000000"
    AUDIO_AUX = b"040255010010701001131000000000000"
    AUDIO_NEXT = b"040255010010701001052000000000000"
    AUDIO_PREV = b"040255010010701001050000000000000"
    AUDIO_PAUSE = b"040255010010701001054000000000000"
    AUDIO_PLAY = b"040255010010701001053000000000000"
    A_TAPE2 = b"040255010010701001148000000000000"
    AUDIO_POWER_OFF = b"040255010010701001012000000000000"


class LinTronicConnection:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def create(cls, url: str) -> Self:
        reader, writer = await serial_asyncio.open_serial_connection(
            url=url, baudrate=19200
        )
        return cls(reader, writer)

    async def listen_for_incoming_messages(self) -> None:
        while True:
            await self.reader.readuntil(START_OF_TRANSMISSION)
            await self.handle_incoming_message()

    async def handle_incoming_message(self) -> None:
        logger.debug("Got incoming message from Lintronic")
        to_address = await self.reader.readexactly(2)
        if to_address != OUR_ADDRESS:
            logger.warning("Received msg not addressed to us, but %s", to_address)
            return

        from_address = await self.reader.readexactly(2)
        if from_address != LINTRONIC_ADDRESS:
            logger.warning(
                "Received msg not sent from Lintronic, but: %s", from_address
            )
            return

        cmd = await self.reader.readexactly(3)
        data_and_checksum = await self.reader.readuntil(END_OF_TRANSMISSION)

        data = data_and_checksum[:-4]
        checksum = data_and_checksum[-3:-1]

        msg = to_address + from_address + cmd + data

        expected_checksum = bytes(f"{sum(c for c in msg) % 256:03}", "ascii")
        if checksum != expected_checksum:
            logger.warning(
                "Got invalid checksum in command %s vs %s", checksum, expected_checksum
            )
            return

        logger.info("Got cmd: %s and data: %s", cmd, data)

    async def write_message_to_lintronic(
        self, beo_command: BeoCommand, repeat_count=1
    ) -> None:
        binary_repeat_count = bytes(f"{repeat_count:03}", "ascii")
        msg = (
            LINTRONIC_ADDRESS
            + OUR_ADDRESS
            + beo_command.value
            + binary_repeat_count
            + MAGIC_SUFFIX
        )

        checksum = bytes(f"{sum(c for c in msg) % 256:03}", "ascii")
        message = START_OF_TRANSMISSION + msg + checksum + END_OF_TRANSMISSION

        logger.debug("Sending message to LinTronic: %s", message)
        self.writer.write(message)
        await self.writer.drain()
