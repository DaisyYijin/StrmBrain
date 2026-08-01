"""
STRMhub 启动入口

直接运行：python main.py
"""
import uvicorn
from app.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
