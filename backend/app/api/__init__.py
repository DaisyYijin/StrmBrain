from app.api.v115 import router as v115_router
from app.api.accounts import router as accounts_router
from app.api.settings import router as settings_router
from app.api.system import router as system_router
from app.api.dashboard import router as dashboard_router
from app.api.organize import router as organize_router
from app.api.tools import router as tools_router
from app.api.wechat import router as wechat_router
from app.api.api_keys import router as ai_router
from app.api.watcher import router as watcher_router
from app.api.emby_webhook import router as emby_webhook_router
from app.api.clouddownload import router as clouddownload_router
from app.api.backup import router as backup_router
from app.api.notification import router as notification_router
from app.api.tasks import router as tasks_router
from app.api.sync_del import router as sync_del_router

__all__ = ["v115_router", "accounts_router", "settings_router", "system_router",
           "dashboard_router", "organize_router", "tools_router", "wechat_router",
           "ai_router", "watcher_router", "emby_webhook_router", "clouddownload_router",
           "backup_router", "notification_router", "tasks_router", "sync_del_router"]
