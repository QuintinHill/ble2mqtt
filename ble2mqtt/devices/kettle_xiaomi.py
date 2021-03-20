import asyncio as aio
import json
import logging
import struct
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from .base import SENSOR_DOMAIN, Device
from .uuids import SOFTWARE_VERSION

logger = logging.getLogger(__name__)


UUID_SERVICE_KETTLE = uuid.UUID('0000fe95-0000-1000-8000-00805f9b34fb')
UUID_SERVICE_KETTLE_DATA = uuid.UUID("01344736-0000-1000-8000-262837236156")
UUID_AUTH_INIT = uuid.UUID('00000010-0000-1000-8000-00805f9b34fb')
UUID_AUTH = uuid.UUID('00000001-0000-1000-8000-00805f9b34fb')
UUID_VER = uuid.UUID('00000004-0000-1000-8000-00805f9b34fb')
UUID_STATUS = uuid.UUID('0000aa02-0000-1000-8000-00805f9b34fb')

TEMPERATURE_ENTITY = 'temperature'
AUTH_MAGIC1 = bytes([0x90, 0xCA, 0x85, 0xDE])
AUTH_MAGIC2 = bytes([0x92, 0xAB, 0x54, 0xFA])

HANDLE_AUTH = 36
HANDLE_STATUS = 60


class Mode(Enum):
    IDLE = 0x00
    HEATING = 0x01
    COOLING = 0x02
    KEEP_WARM = 0x03


class LEDMode(Enum):
    BOIL = 0x01
    KEEP_WARM = 0x02
    NONE = 0xFF


class KeepWarmType(Enum):
    BOIL_AND_COOLDOWN = 0x00
    HEAT_TO_TEMP = 0x01


@dataclass
class MiKettleState:
    mode: Mode = Mode.IDLE
    led_mode: LEDMode = LEDMode.NONE
    temperature: int = 0
    target_temperature: int = 0
    keep_warm_type: KeepWarmType = KeepWarmType.BOIL_AND_COOLDOWN
    keep_warm_time: int = 0

    FORMAT = '<BBHBBBHBBB'

    @classmethod
    def from_bytes(cls, response):
        # 00 ff 00 00 5a 28 00 00 00 01 18 00
        (
            mode,  # 0
            led_mode,  # 1
            _,  # 2-3
            target_temp,  # 4
            current_temp,  # 5
            keep_warm_type,  # 6
            keep_warm_time,  # 7,8
            _, _, _,  # 9, 10, 11
        ) = struct.unpack(cls.FORMAT, response)
        return cls(
            mode=Mode(mode),
            led_mode=LEDMode(led_mode),
            temperature=current_temp,
            target_temperature=target_temp,
            keep_warm_type=KeepWarmType(keep_warm_type),
            keep_warm_time=keep_warm_time,
        )


class XiaomiCipherMixin:
    # Picked from the https://github.com/drndos/mikettle/
    @staticmethod
    def generate_random_token() -> bytes:
        return bytes([  # from component, maybe random is ok
            0x01, 0x5C, 0xCB, 0xA8, 0x80, 0x0A, 0xBD, 0xC1, 0x2E, 0xB8,
            0xED, 0x82,
        ])
        # return os.urandom(12)

    @staticmethod
    def reverse_mac(mac) -> bytes:
        parts = mac.split(":")
        reversed_mac = bytearray()
        length = len(parts)
        for i in range(1, length + 1):
            reversed_mac.extend(bytearray.fromhex(parts[length - i]))
        return reversed_mac

    @staticmethod
    def mix_a(mac, product_id) -> bytes:
        return bytes([
            mac[0], mac[2], mac[5], (product_id & 0xff), (product_id & 0xff),
            mac[4], mac[5], mac[1],
        ])

    @staticmethod
    def mix_b(mac, product_id) -> bytes:
        return bytes([
            mac[0], mac[2], mac[5], ((product_id >> 8) & 0xff), mac[4], mac[0],
            mac[5], (product_id & 0xff),
        ])

    @staticmethod
    def _cipher_init(key) -> bytes:
        perm = bytearray()
        for i in range(0, 256):
            perm.extend(bytes([i & 0xff]))
        keyLen = len(key)
        j = 0
        for i in range(0, 256):
            j += perm[i] + key[i % keyLen]
            j = j & 0xff
            perm[i], perm[j] = perm[j], perm[i]
        return perm

    @staticmethod
    def _cipher_crypt(input, perm) -> bytes:
        index1 = 0
        index2 = 0
        output = bytearray()
        for i in range(0, len(input)):
            index1 = index1 + 1
            index1 = index1 & 0xff
            index2 += perm[index1]
            index2 = index2 & 0xff
            perm[index1], perm[index2] = perm[index2], perm[index1]
            idx = perm[index1] + perm[index2]
            idx = idx & 0xff
            output_byte = input[i] ^ perm[idx]
            output.extend(bytes([output_byte & 0xff]))

        return output

    @classmethod
    def cipher(cls, key, input) -> bytes:
        perm = cls._cipher_init(key)
        return cls._cipher_crypt(input, perm)


class XiaomiKettle(XiaomiCipherMixin, Device):
    NAME = 'mikettle'
    MAC_TYPE = 'random'
    ACTIVE_SLEEP_INTERVAL = 1
    SEND_INTERVAL = 30
    MANUFACTURER = 'Xiaomi'

    def __init__(self, mac, product_id=275, token=None,
                 *args, loop, **kwargs):
        super().__init__(mac, *args, loop=loop, **kwargs)
        self._product_id = product_id
        if token:
            assert isinstance(token, str) and len(token) == 24
            self._token = bytes.fromhex(token)
        else:
            self._token = self.generate_random_token()
        self.queue: aio.Queue = None
        self._state = None

    @property
    def entities(self):
        return {
            SENSOR_DOMAIN: [
                {
                    'name': TEMPERATURE_ENTITY,
                    'device_class': 'temperature',
                    'unit_of_measurement': '\u00b0C',
                },
            ],
        }

    def notification_handler(self, sender: int, data: bytearray):
        logger.debug("Notification: {0}: {1}".format(
            sender,
            ' '.join(format(x, '02x') for x in data),
        ))
        if sender == HANDLE_STATUS:
            self._state = MiKettleState.from_bytes(data)
        else:
            # possible senders: HANDLE_AUTH == 36
            self.queue.put_nowait((sender, data))

    async def auth(self):
        await self.client.write_gatt_char(
            UUID_AUTH_INIT,
            AUTH_MAGIC1,
            True,
        )
        await self.client.start_notify(UUID_AUTH, self.notification_handler)
        await self.client.write_gatt_char(
            UUID_AUTH,
            self.cipher(
                self.mix_a(self.reverse_mac(self.mac), self._product_id),
                self._token,
            ),
            True,
        )
        auth_response = await aio.wait_for(self.queue.get(), timeout=10)
        logger.debug(f'{self} auth response: {auth_response}')
        await self.client.write_gatt_char(
            UUID_AUTH,
            XiaomiCipherMixin.cipher(self._token, AUTH_MAGIC2),
            True,
        )
        await self.client.read_gatt_char(UUID_VER)
        await self.client.stop_notify(UUID_AUTH)

    async def get_device_data(self):
        self.queue = aio.Queue()
        await self.auth()
        self._model = 'MiKettle'
        version = await self.client.read_gatt_char(SOFTWARE_VERSION)
        if version:
            self._version = version.decode()
        logger.debug(f'{self} version: {version}')
        await self.client.start_notify(UUID_STATUS, self.notification_handler)

    async def _notify_state(self, publish_topic):
        logger.info(f'[{self}] send state={self._state}')
        state = {}
        for sensor_name, value in (
            ('temperature', self._state.temperature),
        ):
            if any(
                    x['name'] == sensor_name
                    for x in self.entities.get('sensor', [])
            ):
                state[sensor_name] = self.transform_value(value)
        if state:
            state['linkquality'] = self.linkquality
            await publish_topic(
                topic='/'.join((self.unique_id, 'state')),
                value=json.dumps(state),
            )

    async def handle(self, publish_topic, send_config, *args, **kwargs):
        send_time = None
        while True:
            await self.update_device_data(send_config)
            if self._state and (
                not send_time or
                (time.time() - send_time) > self.SEND_INTERVAL
            ):
                send_time = time.time()
                await self._notify_state(publish_topic)
            await aio.sleep(self.ACTIVE_SLEEP_INTERVAL)