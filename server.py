import asyncio
import websockets
import os

clients = {}  # websocket -> name

async def handler(websocket):
    try:
        # 1️⃣ При подключении клиент должен отправить своё имя
        name = await websocket.recv()
        clients[websocket] = name

        print(f"{name} подключился", flush=True)

        # 2️⃣ Слушаем сообщения
        async for message in websocket:
            print(f"От {name}: {message}", flush=True)

            # 🔔 Сигнал
            if message.startswith("PING"):
                for client in clients:
                    if client != websocket:
                        await client.send("PING")

            # 💬 Чат
            elif message.startswith("CHAT:"):
                _, sender, text = message.split(":", 2)
                for client in clients:
                    await client.send(f"CHAT:{sender}:{text}")

    except websockets.exceptions.ConnectionClosed:
        print(f"{clients.get(websocket, 'Unknown')} отключился", flush=True)

    finally:
        # удаляем клиента
        if websocket in clients:
            del clients[websocket]


# 🚀 Запуск сервера
async def main():
    port = int(os.environ.get("PORT", 8080))  # Railway использует PORT
    async with websockets.serve(
        handler,
        "0.0.0.0",
        port,
        ping_interval=20,
        ping_timeout=20
    ):
        print(f"Server started on port {port}", flush=True)
        await asyncio.Future()  # держим сервер живым


if __name__ == "__main__":
    asyncio.run(main())
