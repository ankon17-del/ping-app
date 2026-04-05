import asyncio
import websockets
import os

# Обработчик сообщений от клиентов
async def handler(websocket):
    print("New client connected")
    try:
        async for message in websocket:
            print(f"Received message: {message}")
            # Отправляем обратно "pong" для проверки
            await websocket.send("pong")
    except websockets.ConnectionClosed:
        print("Client disconnected")

# Основная функция запуска сервера
async def main():
    # Railway назначает порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8765))
    # Слушаем на всех интерфейсах
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"Server started on port {port}")
        await asyncio.Future()  # держим сервер живым

if __name__ == "__main__":
    # Запуск asyncio цикла
    asyncio.run(main())
