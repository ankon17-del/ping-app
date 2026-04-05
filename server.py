from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()

clients = {}

@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    try:
        name = await websocket.receive_text()
        clients[websocket] = name

        print(f"{name} подключился", flush=True)

        while True:
            data = await websocket.receive_text()
            print(f"{name}: {data}", flush=True)

            if data == "PING":
                for client in clients:
                    if client != websocket:
                        await client.send_text("PING")

            elif data.startswith("CHAT:"):
                for client in clients:
                    await client.send_text(data)

    except WebSocketDisconnect:
        print(f"{clients.get(websocket, 'Unknown')} отключился", flush=True)
        clients.pop(websocket, None)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
