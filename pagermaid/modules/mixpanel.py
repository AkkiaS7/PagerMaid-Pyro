import contextlib
import datetime
import json
import time
import uuid

from pagermaid.config import Config
from pagermaid import logs
from pagermaid.enums import Client, Message
from pagermaid.services import client as request
from pagermaid.hook import Hook


class DatetimeSerializer(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            fmt = "%Y-%m-%dT%H:%M:%S"
            return obj.strftime(fmt)

        return json.JSONEncoder.default(self, obj)


class Mixpanel:
    def __init__(self, token: str):
        self._token = token
        self._serializer = DatetimeSerializer
        self._request = request
        self.api_host = "api.mixpanel.com"
        self.is_people_set = False

    @staticmethod
    def _now():
        return time.time()

    @staticmethod
    def _make_insert_id():
        return uuid.uuid4().hex

    @staticmethod
    def json_dumps(data, cls=None):
        # Separators are specified to eliminate whitespace.
        return json.dumps(data, separators=(",", ":"), cls=cls)

    async def api_call(self, endpoint, json_message):
        _endpoints = {
            "events": f"https://{self.api_host}/track",
            "people": f"https://{self.api_host}/engage",
        }
        request_url = _endpoints.get(endpoint)
        if request_url is None:
            return
        params = {
            "data": json_message,
            "verbose": 1,
            "ip": 0,
        }
        start = self._now()
        with contextlib.suppress(Exception):
            await self._request.post(request_url, data=params, timeout=10.0)
        logs.debug(f"Mixpanel request took {self._now() - start} seconds")

    async def people_set(self, distinct_id: str, properties: dict, force_update: bool = False):
        if self.is_people_set and (not force_update):
            return
        message = {
            "$distinct_id": distinct_id,
            "$set": properties,
        }
        record = {"$token": self._token, "$time": self._now()}
        # sourcery skip: dict-assign-update-to-union
        record.update(message)
        res = await self.api_call(
            "people", self.json_dumps(record, cls=self._serializer)
        )
        self.is_people_set = True
        return res

    async def track(self, distinct_id: str, event_name: str, properties: dict):
        all_properties = {
            "token": self._token,
            "distinct_id": distinct_id,
            "time": self._now(),
            "$insert_id": self._make_insert_id(),
            "mp_lib": "python",
            "$lib_version": "4.10.0",
        }
        if properties:
            # sourcery skip: dict-assign-update-to-union
            all_properties.update(properties)
        event = {
            "event": event_name,
            "properties": all_properties,
        }
        return await self.api_call(
            "events", self.json_dumps(event, cls=self._serializer)
        )


mp = Mixpanel(Config.MIXPANEL_API)


async def set_people(bot: Client, force_update: bool = False):
    if not Config.ALLOW_ANALYTIC:
        return
    if mp.is_people_set and (not force_update):
        return
    if not bot.me:
        bot.me = await bot.get_me()
    data = {"$first_name": bot.me.first_name}
    if bot.me.username:
        data["username"] = bot.me.username
    bot.loop.create_task(mp.people_set(str(bot.me.id), data, force_update=force_update))


@Hook.on_startup()
async def mixpanel_init_id(bot: Client):
    if not Config.ALLOW_ANALYTIC:
        return
    await set_people(bot)


@Hook.command_postprocessor()
async def mixpanel_report(bot: Client, message: Message, command):
    if not Config.ALLOW_ANALYTIC:
        return
    await set_people(bot)
    if not bot.me:
        bot.me = await bot.get_me()
    sender_id = message.from_user.id if message.from_user else ""
    sender_id = message.sender_chat.id if message.sender_chat else sender_id
    if sender_id < 0 and message.outgoing:
        sender_id = bot.me.id
    bot.loop.create_task(
        mp.track(
            str(sender_id),
            f"Function {command}",
            {"command": command, "bot_id": bot.me.id},
        )
    )
