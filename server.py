import asyncio
import websockets
import json
import os

# Словарь подключенных клиентов
clients = {}

# URL к папке assets на Railway (поменяй на свой домен)
ASSETS_URL = "https://ping-app-production-6ad4.up.railway.app/assets"

async def handler(ws):
    # Получаем имя пользователя
    name = await ws.recv()
    clients[name] = ws
    print(f"{name} подключился")

    # Уведомляем всех клиентов о статусе
    await broadcast_status()

    try:
        async for msg in ws:
            data = json.loads(msg)

            # --- чат ---
            if data.get("type") == "chat":
                target = data.get("to")
                if target in clients:
                    await clients[target].send(json.dumps({
                        "type": "chat",
                        "from": name,
                        "msg": data.get("msg")
                    }))

            # --- пинг ---
            elif data.get("type") == "ping":
                target = data.get("to")
                if target in clients:
                    await clients[target].send(json.dumps({
                        "type": "ping",
                        "from": name,
                        "sound_url": f"{ASSETS_URL}/ping_sound.mp3",
                        "image_url": f"{ASSETS_URL}/popup_image.png"
                    }))
    finally:
        # Клиент отключился
        if name in clients:
            del clients[name]
        await broadcast_status()

# Функция для обновления статусов всех клиентов
async def broadcast_status():
    status_data = {}
    for user in ["you", "brother"]:
        status_data[user] = user in clients
    for ws in clients.values():
        await ws.send(json.dumps({"type": "status", "status": status_data}))

# Запуск сервера
async def main():
    port = int(os.environ.get("PORT", 8765))  # Railway задаёт PORT автоматически
    async with websockets.serve(handler, "0.0.0.0", port):
        print("Server started")
        await asyncio.Future()  # держим сервер запущенным

asyncio.run(main())