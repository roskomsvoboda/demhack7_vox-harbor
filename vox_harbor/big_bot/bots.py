import asyncio
import datetime
import logging
import random
from typing import Sequence

import cachetools
import pyrogram.errors.exceptions
from aiolimiter import AsyncLimiter
from pyrogram import Client, enums, raw, types, utils
from pyrogram.types.messages_and_media.message import Message as PyrogramMessage

from vox_harbor.big_bot import structures
from vox_harbor.big_bot.chats import ChatsManager
from vox_harbor.big_bot.exceptions import AlreadyJoinedError
from vox_harbor.big_bot.tasks import HistoryTask, TaskManager
from vox_harbor.common.config import Mode, config
from vox_harbor.common.db_utils import db_fetchone, session_scope
from vox_harbor.common.exceptions import format_exception


class Bot(Client):
    INTERVAL = 120
    lock = asyncio.Lock()

    def __init__(self, *args, bot_index, **kwargs):
        super().__init__(*args, sleep_threshold=120, **kwargs)

        self.index = bot_index

        self._invites_callback: dict[str, asyncio.Future[int]] = {}
        self._subscribed_chats: set[int] = set()
        self._subscribed_chats_last_updated = 0

        self.logger = logging.getLogger(f'vox_harbor.big_bot.bots.bot.{bot_index}')

        self.history_limiter = AsyncLimiter(2, 1)

        self.members_count_cache = cachetools.TTLCache(maxsize=10_000, ttl=300)

    async def resolve_invite_callback(self, chat_title: str, channel_id: int):
        self.logger.info('got confirmation for %s', chat_title)
        if chat_title not in self._invites_callback:
            return

        self._invites_callback[chat_title].set_result(channel_id)
        self.logger.info('callback resolved')

    async def update_subscribed_chats(self):
        self.logger.info('updating subscribed chats')
        new_chats = set()

        async for dialog in self.get_dialogs():
            new_chats.add(dialog.chat.id)

        self._subscribed_chats = new_chats
        self._subscribed_chats_last_updated = datetime.datetime.now().timestamp()
        return self._subscribed_chats

    async def get_subscribed_chats(self):
        async with self.lock:
            if (
                not self._subscribed_chats
                or datetime.datetime.now().timestamp() - self._subscribed_chats_last_updated > self.INTERVAL
            ):
                await self.update_subscribed_chats()

            return self._subscribed_chats

    def add_subscribed_chat(self, chat_id: int):
        self._subscribed_chats.add(chat_id)

    async def leave_chat(self, chat_id: int, delete: bool = True):
        self.logger.info('leaving %s', chat_id)
        self._subscribed_chats.remove(chat_id)
        await super().leave_chat(chat_id, delete)

    async def join_chat(self, join_string: str | int):
        if len(self._subscribed_chats) > config.MAX_CHATS_FOR_BOT:
            raise ValueError('Too many chats')

        self.logger.info('joining %s', join_string)
        chat = await super().join_chat(join_string)
        self._subscribed_chats.add(chat.id)
        return chat

    async def discover_chat(
        self,
        join_string: str,
        with_linked: bool = True,
        join_no_check: bool = False,
        ignore_protection: bool = False,
    ):
        try:
            join_string = int(join_string)
        except ValueError:
            pass

        if join_string == 777000:
            return

        self.logger.info('discovering chat %s', join_string)
        preview = await self.get_chat(join_string)
        self.logger.info('chat title %s', preview.title)

        if not ignore_protection:
            if preview.type == enums.ChatType.CHANNEL and preview.members_count < config.MIN_CHANNEL_MEMBERS_COUNT:
                self.logger.info('not enough members to join channel, skip')
                return

            elif preview.type != enums.ChatType.CHANNEL and preview.members_count < config.MIN_CHAT_MEMBERS_COUNT:
                self.logger.info('not enough members to join chat, skip')
                return

        if isinstance(preview, types.Chat):
            chat = preview
        else:
            try:
                chat = await super().join_chat(join_string)
                chat = await self.get_chat(chat.id)  # thank you, telegram
            except pyrogram.errors.exceptions.bad_request_400.InviteRequestSent:
                self.logger.info('waiting for an approval')
                future = asyncio.get_running_loop().create_future()

                try:
                    self._invites_callback[preview.title] = future

                    async with asyncio.timeout(10):
                        chat_id = await future
                        chat = await self.get_chat(chat_id)
                finally:
                    del self._invites_callback[preview.title]

        self.logger.info('discovered chat with id %s', chat.id)

        if join_no_check:  # direct join to the chat in case if we are loading from ChatsManager
            await self.join_chat(chat.id)
        else:
            await self.try_join_discovered_chat(chat, str(join_string))

        if with_linked and chat.linked_chat:
            linked_join_string = chat.linked_chat.username or ''
            await self.discover_chat(linked_join_string, with_linked=False, ignore_protection=ignore_protection, join_no_check=join_no_check)

    async def try_join_discovered_chat(self, chat: types.Chat, join_string: str):
        if chat.id == 777000:
            return

        chats = await ChatsManager.get_instance(await BotManager.get_instance())
        if known_chat := chats.known_chats.get(chat.id):
            if known_chat.shard == config.SHARD_NUM and known_chat.bot_index == self.index:
                if chat.id not in self._subscribed_chats:
                    await self.join_chat(chat.id)

                return

            if chat.id in self._subscribed_chats:
                self.logger.info(
                    'this chat is already handled by another bot (%s), index=%s, leaving', known_chat, self.index
                )
                try:
                    await self.leave_chat(chat.id)
                except Exception as e:
                    self.logger.error('failed to leave chat %s: %s', chat.id, format_exception(e))
        else:
            if chat.id not in self._subscribed_chats:
                await self.join_chat(chat.id)

            await chats.register_new_chat(self.index, chat.id, join_string)

    async def get_message_witch_cache(self, chat_id: int, message_id: int):
        if (chat_id, message_id) in self.message_cache.store:
            return self.message_cache[chat_id, message_id]

        return await self.get_messages(chat_id=chat_id, message_ids=message_id, replies=0)

    async def get_history(self, chat_id: int, start: int, end: int, limit: int) -> list[types.Message]:
        await self.history_limiter.acquire()
        raw_messages = await self.invoke(
            raw.functions.messages.GetHistory(
                peer=await self.resolve_peer(chat_id),
                offset_id=start,
                offset_date=0,
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=end,
                hash=0,
            ),
            sleep_threshold=60,
        )

        return await utils.parse_messages(self, raw_messages, replies=0)

    async def generate_history_task(self, chats: ChatsManager, chat_id: int, with_from_earliest: bool = True):
        tasks = await TaskManager.get_instance()
        comment = await db_fetchone(
            structures.CommentRange,
            'SELECT chat_id, min(min_message_id) as min_message_id, max(max_message_id) as max_message_id FROM comments_range_mv WHERE chat_id = %(chat_id)s\n'
            'GROUP BY chat_id',
            dict(chat_id=chat_id),
            raise_not_found=False,
        )

        if not (chat := chats.known_chats.get(chat_id)):
            return

        if chat.type in (structures.Chat.Type.CHANNEL, structures.Chat.Type.PRIVATE):
            return

        if comment is None:
            await tasks.add_task(HistoryTask(bot=self, chat_id=chat_id, start_id=0, end_id=0))
        else:
            if comment.max_message_id and with_from_earliest:
                await tasks.add_task(HistoryTask(bot=self, chat_id=chat_id, start_id=0, end_id=comment.max_message_id))

            if comment.min_message_id > 1000:
                await tasks.add_task(HistoryTask(bot=self, chat_id=chat_id, start_id=comment.min_message_id, end_id=0))

    async def get_chat_members_count_with_cache(self, chat_id: int | str) -> int:
        if chat_id in self.members_count_cache:
            return self.members_count_cache.get(chat_id)

        count = await self.get_chat_members_count(chat_id)
        self.members_count_cache[chat_id] = count
        return count


class BotManager:
    """
    Universal multi-bot manager. On creation loads available bots from ClickHouse table.
    """

    logger = logging.getLogger('vox_harbor.big_bot.bots')
    lock = asyncio.Lock()

    def __init__(self, bots: list[Bot]):
        self.started: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        self.bots = bots

        self._discover_cache = cachetools.TTLCache(maxsize=500, ttl=60)

    def __getitem__(self, item):
        return self.bots[item]

    def __iter__(self):
        return iter(self.bots)

    async def start(self):
        for bot in self.bots:
            await bot.start()
        self.started.set_result(True)

    async def stop(self):
        for bot in self.bots:
            await bot.stop()

    def register_handler(self, handler, group_id: int = 0):
        for bot in self.bots:
            bot.add_handler(handler, group_id)

    async def update_subscribe_chats(self):
        for bot in self.bots:
            await bot.update_subscribed_chats()

    async def discover_chat(self, join_string: str, ignore_protection: bool = False):
        async with self.lock:
            if join_string in self._discover_cache:
                raise AlreadyJoinedError('chat is being discovered')

            self._discover_cache[join_string] = True

        total = sum([len(await bot.get_subscribed_chats()) for bot in self.bots])
        weights = [total / len(await bot.get_subscribed_chats()) for bot in self.bots]
        bot: Bot = random.choices(self.bots, weights=weights)[0]
        return await bot.discover_chat(join_string, ignore_protection=ignore_protection)

    async def get_messages(self, bot_index: int, chat_id: int | str, message_ids: Sequence[int]) -> list[PyrogramMessage | None]:
        try:
            return await self.bots[bot_index].get_messages(chat_id, message_ids=message_ids)  # type: ignore
        except pyrogram.errors.exceptions.bad_request_400.BadRequest:
            return [None] * len(message_ids)

    @classmethod
    async def get_instance(cls, shard: int = config.SHARD_NUM) -> 'BotManager':
        global _manager

        if _manager is not None:
            return _manager

        if config.MODE == Mode.PROD:
            target_table = 'bots'
        elif config.MODE == Mode.DEV_1:
            target_table = 'bots_dev_1'
        elif config.MODE == Mode.DEV_2:
            target_table = 'bots_dev_2'
        else:
            raise ValueError(f'Unknown mode {config.MODE}')

        cls.logger.info(f'loading bots from table {target_table}')
        async with session_scope() as session:
            # noinspection SqlResolve
            await session.execute(
                f'SELECT * FROM {target_table} WHERE shard == %(shard)s ORDER BY id', dict(shard=shard)
            )
            bots_data = structures.Bot.from_rows(await session.fetchall())

            if len(bots_data) < config.ACTIVE_BOTS_COUNT:
                raise ValueError('Not enough bots to start up')

            await session.execute('SELECT * FROM broken_bots')
            broken_bots_data = structures.BrokenBot.from_rows(await session.fetchall())

        broken_bots_set = {b.id for b in broken_bots_data}

        j = config.ACTIVE_BOTS_COUNT
        for i, bot in enumerate(bots_data):
            if i >= config.ACTIVE_BOTS_COUNT:
                break

            if bot.id in broken_bots_set:
                while j < len(bots_data) and bots_data[j].id in broken_bots_set:
                    j += 1

                if j >= len(bots_data):
                    raise ValueError('Not enough active bots to startup')

                bots_data[i] = bots_data[j]

        bots_data = bots_data[: config.ACTIVE_BOTS_COUNT]

        bots: list[Bot] = []
        for i, bot in enumerate(bots_data):
            bots.append(Bot(bot.name, session_string=bot.session_string, bot_index=i))
            cls.logger.info('loaded bot %s', bot.name)

        _manager = cls(bots)
        return _manager


_manager: BotManager | None = None
