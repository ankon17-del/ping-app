import asyncio
import websockets
import os

connected_clients = {}  # websocket → username

async def handler(websocket):
    try:
        # Получаем имя пользователя при подключении
        username = await websocket.recv()
        connected_clients[websocket] = username
        print(f"New client connected: {username}")

        async for message in websocket:
            print(f"Received from {username}: {message}")

            if message.lower() == "ping":
                # Рассылаем Ping всем кроме отправителя
                for client in connected_clients:
                    if client != websocket:
                        await client.send(f"Ping from {username}")
            else:
                # Рассылаем чат всем
                for client in connected_clients:
                    await client.send(f"{username}: {message}")

    except websockets.ConnectionClosed:
        print(f"Client {connected_clients.get(websocket,'?')} disconnected")
    finally:
        connected_clients.pop(websocket, None)

async def main():
    port = int(os.environ.get("PORT", 8765))  # Railway назначает PORT
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"Server started on port {port}")
        await asyncio.Future()  # держим сервер живым

if __name__ == "__main__":
    asyncio.run(main())
