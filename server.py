# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import asyncio

app = FastAPI()

connected_clients = set()  # {(websocket, username)}
message_history = []  # список всех сообщений

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        # Получаем имя пользователя при подключении
        username = await websocket.receive_text()
        connected_clients.add((websocket, username))
        print(f"New client connected: {username}")
        
        # Отправляем историю сообщений новому клиенту
        for msg in message_history:
            await websocket.send_text(msg)
        
        while True:
            msg = await websocket.receive_text()
            
            # Обработка Ping кнопки
            if msg.startswith("__PING__"):
                # Отправляем Ping другим пользователям
                for ws, name in connected_clients:
                    if ws != websocket:
                        await ws.send_text(f"__PING__::{username}")
                continue

            # Обычное сообщение
            full_msg = f"{username}: {msg}"
            message_history.append(full_msg)
            
            # Рассылаем всем клиентам
            for ws, name in connected_clients:
                await ws.send_text(full_msg)
                
    except WebSocketDisconnect:
        connected_clients.remove((websocket, username))
        print(f"Client {username} disconnected")
        # Сообщаем остальным пользователям об отключении
        for ws, name in connected_clients:
            await ws.send_text(f"__DISCONNECT__::{username}")
