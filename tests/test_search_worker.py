# ---------- Снимок выдачи в воркере: ключи очередей и обработчик ----------
from app.core.exceptions import AntibotBlockedError
from app.worker.handlers import handle_search
from app.worker.redis_config import RedisWorkerSettings

TASK = {"task_id": "t-1", "set_id": "s-1", "stratum_id": "st-1", "geo": "Санкт-Петербург",
        "marketplace": "ozon", "category": "kids_clothing", "query": "Блузка для девочки",
        "run_at": "2026-10-06T18:37:00+03:00"}


class FakeService:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = []

    def search_tiles(self, query, target=None, save_files=True):
        self.calls.append((query, target, save_files))
        if self.exc:
            raise self.exc
        return {"query": query, "url": "https://www.ozon.ru/search/?text=x", "scrolls": 1,
                "tiles_total": 1, "tiles": [{"position": 1, "sku": "123"}], "debug_files": {}}


def test_search_keys_and_priority():
    s = RedisWorkerSettings(PARSER_MARKETPLACES="ozon", OZON_GEO_CODE="spb")
    assert s.task_keys == ["select:tasks:ozon:spb", "search:tasks:ozon:spb", "parse:tasks:ozon:spb"]
    assert s.key_routes["search:tasks:ozon:spb"] == ("search", "ozon")
    assert s.search_results_key("ozon") == "search:results:ozon:spb"


def test_handle_search_ok_echoes_task():
    svc = FakeService()
    r = handle_search({**TASK, "target": 20}, svc)
    assert r["ok"] is True and r["error"] is None
    assert (r["task_id"], r["stratum_id"], r["query"]) == ("t-1", "st-1", "Блузка для девочки")
    assert r["tiles_total"] == 1 and r["tiles"][0]["sku"] == "123"
    assert r["parsed_at"]
    assert (r["category"], r["run_at"]) == ("kids_clothing", "2026-10-06T18:37:00+03:00")
    assert svc.calls[0][:2] == ("Блузка для девочки", 20)


def test_handle_search_antibot():
    r = handle_search(TASK, FakeService(AntibotBlockedError("капча", marketplace="ozon")))
    assert r["ok"] is False and r["error_code"] == "antibot_blocked"
    assert r["tiles"] == [] and r["task_id"] == "t-1"


def test_handle_search_without_query():
    r = handle_search({**TASK, "query": None}, FakeService())
    assert r["ok"] is False and r["error_code"] == "invalid_request"