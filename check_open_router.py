import asyncio
import httpx
from app.config import get_settings

async def main():
    settings = get_settings()

    response = await httpx.AsyncClient().post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.openrouter_model,
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "Return exactly OK"}
            ],
        },
    )

    print(response.status_code)
    print(response.text)

asyncio.run(main())