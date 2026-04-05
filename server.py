from fastapi import FastAPI, WebSocket
import asyncio

app = FastAPI()
connected_clients = {}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    username = await websocket.receive_text()
    connected_clients[websocket] = username
    print(f"New client connected: {username}")

    try:
        while True:
            data = await websocket.receive_text()
            if data.lower() == "ping":
                for client in connected_clients:
                    if client != websocket:
                        await client.send_text(f"Ping from {username}")
            else:
                for client in connected_clients:
                    await client.send_text(f"{username}: {data}")
    except Exception:
        pass
    finally:
        print(f"Client {connected_clients.get(websocket,'?')} disconnected")
        connected_clients.pop(websocket, None)
