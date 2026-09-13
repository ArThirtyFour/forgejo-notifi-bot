import contextlib
import logging

from aerich import Command
from tortoise import Tortoise


async def create_models(tortoise_config: dict):
    try:
        command = Command(tortoise_config=tortoise_config, app="models")
        await command.init()
        await command.init_db(safe=True)
        await command.upgrade(run_in_transaction=True)
    except Exception as e:
        logging.info("create_models init: %s", e)


async def migrate_models(tortoise_config: dict):
    try:
        command = Command(tortoise_config=tortoise_config, app="models")
        await command.init()
        with contextlib.suppress(Exception):
            await command.migrate()
        with contextlib.suppress(Exception):
            await command.upgrade(run_in_transaction=True)
    except Exception as e:
        logging.info("migrate_models info: %s", e)


async def init_orm(tortoise_config: dict) -> None:
    await Tortoise.init(config=tortoise_config)
    await Tortoise.generate_schemas(safe=True)
    logging.info(f"Tortoise-ORM started, {Tortoise.apps}")


async def close_orm() -> None:
    await Tortoise.close_connections()
    logging.info("Tortoise-ORM shutdown")
