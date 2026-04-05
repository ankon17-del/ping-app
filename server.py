import asyncio
import websockets
import os

connected_clients = {}  # {websocket: username}

async def handler(websocket):
    try:
        # Получаем имя пользователя при подключении
        name = await websocket.recv()
        connected_clients[websocket] = name
        print(f"{name} подключился")

        # Отправляем всем сообщение о новом подключении
        for client in connected_clients:
            if client != websocket:
                await client.send(f"Сервер: {name} присоединился к чату")

        async for message in websocket:
            print(f"{name} пишет: {message}")
            # Рассылаем всем клиентам
            for client in connected_clients:
                if client != websocket:
                    await client.send(f"{name}: {message}")
    except websockets.ConnectionClosed:
        print(f"{connected_clients.get(websocket, 'Неизвестный')} отключился")
    finally:
        if websocket in connected_clients:
            left_name = connected_clients[websocket]
            del connected_clients[websocket]
            # Сообщаем всем о выходе пользователя
            for client in connected_clients:
                await client.send(f"Сервер: {left_name} покинул чат")

async def main():
    port = int(os.environ.get("PORT", 8765))
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"Server started on port {port}")
        await asyncio.Future()  # держим сервер живым

if __name__ == "__main__":
    asyncio.run(main())
