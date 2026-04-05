import os
import asyncio
from fastapi import FastAPI, WebSocket
import uvicorn

app = FastAPI()

clients = set()

# HTTP проверка статуса (GET /)
@app.get("/")
async def root():
    return {"status": "ok"}  # теперь Railway не будет выдавать 502

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            else:
                # эхо обратно
                await websocket.send_text(data)
    except:
        clients.remove(websocket)
        await websocket.close()

# Запуск сервера
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))  # Railway назначает PORT
    uvicorn.run(app, host="0.0.0.0", port=port)
