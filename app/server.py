"""Application entrypoint."""
from app.main import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    from app.config import get_settings
    cfg = get_settings()
    uvicorn.run("app.server:app", host=cfg.host, port=cfg.port, reload=cfg.app_env == "development")
