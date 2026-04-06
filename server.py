from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import asyncio
import json
import uuid

app = FastAPI()

connected_clients = set()  # {(websocket, username, user_id)}
message_history = []  # последние 100 сообщений

MAX_HISTORY = 100  # ограничение истории

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        username = await websocket.receive_text()
        user_id = str(uuid.uuid4())  # временно уникальный id
        connected_clients.add((websocket, username, user_id))
        print(f"New client connected: {username} ({user_id})")

        # Отправка истории новому клиенту
        for msg in message_history:
            await websocket.send_text(json.dumps(msg))
        
        # Системное уведомление о подключении
        system_msg = {
            "type": "system",
            "text": f"{username} подключился",
            "user": username
        }
        await broadcast(system_msg, exclude=[websocket])

        while True:
            raw_msg = await websocket.receive_text()
            
            if raw_msg.startswith("__PING__"):
                ping_msg = {
                    "type": "ping",
                    "user": username,
                    "text": "Ping!"
                }
                await broadcast(ping_msg, exclude=[websocket])
                continue

            # Обычное сообщение
            chat_msg = {
                "type": "message",
                "user": username,
                "user_id": user_id,
                "text": raw_msg
            }

            message_history.append(chat_msg)
            if len(message_history) > MAX_HISTORY:
                message_history.pop(0)

            await broadcast(chat_msg)
                
    except WebSocketDisconnect:
        connected_clients.remove((websocket, username, user_id))
        print(f"Client {username} disconnected")

        disc_msg = {
            "type": "system",
            "text": f"{username} отключился",
            "user": username
        }
        await broadcast(disc_msg)

# --------------------------
# Вспомогательная функция рассылки
# --------------------------
async def broadcast(message: dict, exclude=[]):
    to_remove = []
    for ws, _, _ in connected_clients:
        if ws in exclude:
            continue
        try:
            await ws.send_text(json.dumps(message))
        except:
            to_remove.append(ws)
    # удаляем нерабочие соединения
    for ws in to_remove:
        for c in connected_clients.copy():
            if c[0] == ws:
                connected_clients.remove(c)
