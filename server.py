# server.py
import asyncio
from fastapi import FastAPI, WebSocket
import os

app = FastAPI()

# Для проверки HTTP
@app.get("/")
async def root():
    return {"status": "ok"}

# WebSocket путь
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text("pong")
    except Exception:
        await websocket.close()
