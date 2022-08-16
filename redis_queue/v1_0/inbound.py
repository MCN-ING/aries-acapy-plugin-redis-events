import asyncio
import base64
import json
from json import JSONDecodeError
import logging
from typing import cast
from uuid import uuid4

from aries_cloudagent.messaging.error import MessageParseError
from aries_cloudagent.transport.error import WireFormatParseError
from aries_cloudagent.transport.inbound.base import (
    BaseInboundTransport,
    InboundTransportError,
)
from aries_cloudagent.transport.wire_format import (
    DIDCOMM_V0_MIME_TYPE,
    DIDCOMM_V1_MIME_TYPE,
)
from redis.asyncio import RedisCluster
from redis.exceptions import RedisError

from utils import (
    str_to_datetime,
    curr_datetime_to_str,
    get_timedelta_seconds,
    b64_to_bytes,
)

from .config import get_config, InboundConfig

LOGGER = logging.getLogger(__name__)


class RedisInboundTransport(BaseInboundTransport):
    """Inbound Transport using Redis."""

    def __init__(self, host: str, port: int, create_session, **kwargs) -> None:
        """
        Initialize an inbound HTTP transport instance.

        Args:
            host: Host to listen on
            port: Port to listen on
            create_session: Method to create a new inbound session

        """
        super().__init__("redis", create_session, **kwargs)
        self.host = host
        self.port = port
        self.running = True
        self.inbcound_config = (
            get_config(self.root_profile.context.settings).inbound
            or InboundConfig.default()
        )
        self.redis = self.root_profile.inject(RedisCluster)
        self.inbound_topic = self.inbcound_config.acapy_inbound_topic
        self.direct_response_topic = self.inbcound_config.acapy_direct_resp_topic

    async def start(self):
        plugin_uid = str(uuid4()).encode("utf-8")
        new_recip_keys_set = base64.urlsafe_b64encode(
            json.dumps([]).encode("utf-8")
        ).decode()
        await self.redis.hset("uid_recip_keys_map", plugin_uid, new_recip_keys_set)
        retry_counter = 0
        LOGGER.info(f"New plugin instance {plugin_uid.decode()} setup")
        while self.running:
            try:
                recip_keys_encoded = await self.redis.hget(
                    "uid_recip_keys_map", plugin_uid
                )
                if not recip_keys_encoded:
                    await asyncio.sleep(0.2)
                    continue
                inbound_msg_keys_set = json.loads(
                    b64_to_bytes(recip_keys_encoded).decode()
                )
                retry_counter = 0
            except (TypeError, RedisError) as err:
                if retry_counter >= 5:
                    print(f"Raise Error {str(err)}, UID: " + plugin_uid.decode())
                retry_counter = retry_counter + 1
                await asyncio.sleep(3)
                continue
            for recip_key in inbound_msg_keys_set:
                msg_received = False
                retry_pop_count = 0
                while not msg_received:
                    try:
                        msg_bytes = await self.redis.blpop(self.inbound_topic, 0.2)
                        msg_received = True
                        retry_pop_count = 0
                    except RedisError as err:
                        await asyncio.sleep(1)
                        LOGGER.warning(err)
                        retry_pop_count = retry_pop_count + 1
                        if retry_pop_count > 5:
                            raise InboundTransportError(
                                f"Unexpected exception {str(err)}"
                            )
                if not msg_bytes:
                    await asyncio.sleep(1)
                    continue
                msg_bytes = msg_bytes[1]
                try:
                    inbound = json.loads(msg_bytes)
                    payload = base64.urlsafe_b64decode(inbound["payload"])
                except (JSONDecodeError, KeyError):
                    LOGGER.exception("Received invalid inbound message record")
                await self.redis.hset(
                    "uid_last_access_map",
                    plugin_uid,
                    curr_datetime_to_str().encode("utf-8"),
                )
                uid_recip_key = f"{plugin_uid.decode()}_{recip_key}".encode("utf-8")
                enc_uid_recip_key_count = await self.redis.hget(
                    "uid_recip_key_pending_msg_count", uid_recip_key
                )
                if (
                    enc_uid_recip_key_count
                    and int(enc_uid_recip_key_count.decode()) >= 1
                ):
                    await self.redis.hset(
                        "uid_recip_key_pending_msg_count",
                        uid_recip_key,
                        (int(enc_uid_recip_key_count.decode()) - 1),
                    )
                try:
                    direct_reponse_requested = True if "txn_id" in inbound else False
                    session = await self.create_session(
                        accept_undelivered=False, can_respond=False
                    )
                    async with session:
                        await session.receive(cast(bytes, payload))
                        if direct_reponse_requested:
                            txn_id = inbound["txn_id"]
                            response = await session.wait_response()
                            response_data = {}
                            if response:
                                if isinstance(response, bytes):
                                    if session.profile.settings.get(
                                        "emit_new_didcomm_mime_type"
                                    ):
                                        response_data[
                                            "content-type"
                                        ] = DIDCOMM_V1_MIME_TYPE
                                    else:
                                        response_data[
                                            "content-type"
                                        ] = DIDCOMM_V0_MIME_TYPE
                                else:
                                    response_data["content-type"] = "application/json"
                            response_data["response"] = base64.urlsafe_b64encode(
                                response
                            ).decode()
                            message = {}
                            message["txn_id"] = txn_id
                            message["response_data"] = response_data
                            try:
                                self.redis.rpush(
                                    self.direct_response_topic,
                                    str.encode(json.dumps(message)),
                                )
                            except RedisError as err:
                                LOGGER.exception(f"Unexpected exception {str(err)}")
                except (JSONDecodeError, KeyError):
                    LOGGER.exception("Received invalid inbound message record")
                except (MessageParseError, WireFormatParseError):
                    LOGGER.exception("Failed to process message")

    async def stop(self):
        pass
