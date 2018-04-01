import json
import logging

from aiohttp import web, WSMsgType

from .bot import BotConnection
from .client import ClientConnection
from .models import Task


class Pool(object):
    def __init__(self):
        self._config = json.loads(open("config.json").read())["pool"]
        self.routes = [
            web.get(self._config["endpoint"], self.process_bot)
        ]
        self._pending_tasks = Task.get_uncompleted()
        self._log("Pending tasks %s" % self._pending_tasks)
        self.sessions = {}
        self._bots = []

    @staticmethod
    def _log(message):
        logging.info("[POOL] %s" % message)

    def _get_optimal_bot(self) -> BotConnection:
        """Sort bots by current number of tasks and return the one with least tasks"""

        self._bots.sort(key=lambda x: x.tasks, reverse=True)
        self._bots[0].tasks += 1

        return self._bots[0]

    async def send_task(self, client_connection: ClientConnection, task: dict):
        """Adds new task to DB and sends it to Bot"""

        Task.create(
            session=client_connection.session,
            data=task)

        if len(self._bots) == 0:
            self._log("No available bots. Caching task")
            await client_connection.send_error("no available bots")
            self._pending_tasks.append(task)

        else:
            await self._get_optimal_bot().send_task(task)

    async def broadcast(self, message: str):
        """Sends a message to all connected clients"""

        for bot in self._bots:
            try:
                await bot.send_json(message)
            except RuntimeError as e:
                self._log(e)
                self._bots.remove(bot)
            except ValueError as e:
                self._log(e)

    async def process_bot(self, request: web.Request) -> web.WebSocketResponse:
        """Appends bot to the pool and listens to incoming messages"""

        self._log("New bot connection")

        ws = web.WebSocketResponse(
            heartbeat=self._config["ping_interval"] if self._config["ping_enabled"] else None
        )
        await ws.prepare(request)

        bot = BotConnection(connection=ws)
        self._bots.append(bot)

        if len(self._pending_tasks) > 0:
            self._log("Sending pending tasks")
            for task in self._pending_tasks:
                await bot.send_task(task)

        async for message in ws:

            if message.type == WSMsgType.text:
                self._log("Bot send %s" % message.data)
                await self._process_message(bot, message.data)

            elif message.type == WSMsgType.CLOSE or message.type == WSMsgType.ERROR:
                self._log("Bot disconnected")
                await ws.close()
                self._bots.remove(bot)

        return ws

    async def _process_message(self, bot: BotConnection, message: str):
        bot.tasks -= 1
        message = json.loads(message)

        if message["session_id"] not in self.sessions:
            self._log("Response ready but client not found")
        else:
            self._log("Response ready and sent to client")
            await self.sessions[message["session_id"]].send_response(message)
