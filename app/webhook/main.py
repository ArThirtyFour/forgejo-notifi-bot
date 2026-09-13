from fastapi import FastAPI
import uvicorn

from app.config import Config
from app.webhook.api import router as webhook_router, set_config


def dispatcher(config: Config | None = None):
    if config is not None:
        set_config(config)

    app = FastAPI()
    app.include_router(router=webhook_router, prefix="/webhook")

    @app.get("/")
    async def root():
        return {"status": "ok", "service": "forgejo-notifier-bot"}

    uvicorn.run(app, host="0.0.0.0", port=4454)
