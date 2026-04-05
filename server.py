# server.py
import os
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

# Разрешаем CORS для всех (для теста EXE-клиентов)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Простая проверка HTTP
@app.get("/")
async def root():
    return {"status": "ok"}

# WebSocket путь
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("New client connected")
    try:
        while True:
            message = await websocket.receive_text()
            print(f"Received: {message}")
            # Отправляем обратно pong
            await websocket.send_text("pong")
    except Exception:
        print("Client disconnected")
        await websocket.close()

# Запуск через uvicorn
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
