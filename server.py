import os
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI()
clients = set()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        # Получаем имя пользователя при подключении
        name = await websocket.receive_text()
        print(f"New client connected: {name}")
        clients.add((websocket, name))

        while True:
            message = await websocket.receive_text()
            print(f"Received from {name}: {message}")
            # Отправляем всем клиентам сообщение
            for client, _ in clients:
                await client.send_text(f"{name}: {message}")
            # Отправка "pong" для кнопки пинг
            if message.lower() == "ping":
                await websocket.send_text("pong")

    except Exception as e:
        print(f"Client {name} disconnected: {e}")
    finally:
        clients.remove((websocket, name))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))  # Railway назначает порт
    uvicorn.run(app, host="0.0.0.0", port=port)
