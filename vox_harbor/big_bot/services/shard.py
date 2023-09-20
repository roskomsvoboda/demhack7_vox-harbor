import asyncio
from itertools import chain, groupby
from operator import attrgetter

from fastapi import APIRouter

from vox_harbor.big_bot.bots import BotManager
from vox_harbor.big_bot.configs import Config
from vox_harbor.big_bot.structures import Comment, Message, ShardLoad

shard_router = APIRouter()


@shard_router.post('/messages')
async def get_messages(sorted_comments: list[Comment]) -> list[Message]:
    bot_manager = await BotManager.get_instance(Config.SHARD_NUM)
    tasks = []

    for bot_index, comments_by_bot_index in groupby(sorted_comments, attrgetter('bot_index')):
        for chat_id, comments_by_chat_id in groupby(comments_by_bot_index, attrgetter('chat_id')):
            message_ids = [msg.message_id for msg in comments_by_chat_id]
            get_msgs = bot_manager.get_messages(bot_index, chat_id, message_ids)
            tasks.append(get_msgs)

    pyrogram_messages = chain.from_iterable(await asyncio.gather(*tasks))
    # fixme len(msgs) < len(comments) ; use fields of pyrogram_messages
    messages_zipped = zip(pyrogram_messages, sorted_comments, strict=True)
    return [Message(text=msg.text, comment=cmt) for msg, cmt in messages_zipped]


@shard_router.get('/load')
async def get_load() -> ShardLoad:
    ...  # todo


@shard_router.post('/discover')
async def discover(join_string: str, bot_index: int) -> None:
    ...  # todo