import asyncio
import websockets
import json

clients = {}

async def handler(websocket):
    name = await websocket.recv()
    clients[name] = websocket
    print(f"{name} подключился")

    try:
        async for message in websocket:
            data = json.loads(message)
            target = data.get("to")

            if target in clients:
                await clients[target].send(json.dumps({
                    "from": name,
                    "type": "ping"
                }))
    except:
        pass
    finally:
        print(f"{name} отключился")
        del clients[name]

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        print("Сервер запущен")
        await asyncio.Future()

asyncio.run(main())