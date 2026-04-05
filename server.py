import os
import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()
clients = set()

# CORS для безопасного теста, если нужно
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    print(f"{websocket.client} connected")
    try:
        while True:
            data = await websocket.receive_text()
            # Просто отправляем обратно всем клиентам
            for client in clients:
                await client.send_text(f"Ping from {data}")
    except:
        clients.remove(websocket)
        print(f"{websocket.client} disconnected")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
