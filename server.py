import asyncio
import websockets
import os

connected_clients = set()
message_history = []

async def handler(websocket):
    # Получаем имя пользователя при подключении
    username = await websocket.recv()
    print(f"New client connected: {username}")
    connected_clients.add(websocket)

    # Отправляем историю сообщений новому клиенту
    for msg in message_history:
        await websocket.send(msg)

    try:
        async for message in websocket:
            # Спецкоманда ping
            if message == "__PING__":
                for client in connected_clients:
                    if client != websocket:
                        await client.send(f"__PING__::{username}")
                continue

            # обычное сообщение
            full_msg = f"{username}: {message}"
            message_history.append(full_msg)

            # рассылаем всем
            for client in connected_clients:
                await client.send(full_msg)

    except websockets.ConnectionClosed:
        print(f"Client {username} disconnected")
    finally:
        connected_clients.remove(websocket)

async def main():
    port = int(os.environ.get("PORT", 8765))
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"Server started on port {port}")
        await asyncio.Future()  # держим сервер живым

if __name__ == "__main__":
    asyncio.run(main())
