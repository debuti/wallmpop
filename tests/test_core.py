import asyncio

import pytest

from wallmpop.db import DB
from wallmpop.health import SourceHealth
from wallmpop.judge import parse_verdicts
from wallmpop.llm import extract_json
from wallmpop.models import AlertConfig, Listing
from wallmpop.prefilter import prefilter
from wallmpop.scheduler import Scheduler
from wallmpop.sources import SourceError
from wallmpop.sources.wallapop import build_params, parse_response


def cfg(**kw):
    base = dict(name="t", query="nvme 512", rubric="r", price_max=50, radius_km=30,
                include_regex=[r"512\s?gb?"], exclude_regex=[r"\b(busco|roto)\b"])
    base.update(kw)
    return AlertConfig.from_dict(base)


def item(id="a", price=40, title="NVMe 512GB", desc="perfecto", seller="s1", **kw):
    return Listing(id=id, title=title, description=desc, price=price, seller_id=seller,
                   url="u", lat=40.42, lon=-3.70, **kw)


def test_prefilter():
    c = cfg()
    assert prefilter(c, item()) is None
    assert prefilter(c, item(price=60)) == "precio alto"
    assert prefilter(c, item(title="NVMe 1TB")).startswith("no casa")
    assert prefilter(c, item(desc="busco uno")).startswith("excluido")
    assert prefilter(c, item(reserved=True)) == "reservado"
    assert prefilter(c, item(), muted={"s1"}) == "vendedor silenciado"
    far = item(); far.lat, far.lon = 41.39, 2.17  # Barcelona
    assert prefilter(c, far) == "lejos"


def test_config_validation():
    with pytest.raises(ValueError):
        AlertConfig.from_dict({"name": "x", "query": "y"})
    with pytest.raises(ValueError):
        cfg(include_regex=["(unclosed"])
    assert cfg(interval_min=1).interval_min == 5
    p = build_params(cfg(price_min=10))
    assert p["distance_in_km"] == 30 and p["max_sale_price"] == 50 and p["min_sale_price"] == 10


def test_parse_response():
    good = {"data": {"section": {"payload": {"items": [
        {"id": "x1", "title": "t", "price": {"amount": 10}, "user_id": "u", "web_slug": "t-1"}]}}}}
    [it] = parse_response(good)
    assert it.price == 10 and it.url.endswith("/item/t-1")
    with pytest.raises(SourceError) as e:
        parse_response({"items": []})
    assert e.value.kind == "schema"
    with pytest.raises(SourceError):
        parse_response({"data": {"section": {"payload": {"items": [{"title": "no id"}] * 3}}}})


def test_verdicts_and_json():
    data = extract_json('```json\n[{"id":"a","v":"m","s":8,"why":"ok"},{"id":"zz","v":"m"}]\n```')
    v = parse_verdicts(data, {"a", "b"})
    assert v == {"a": {"verdict": "match", "score": 8.0, "why": "ok"}}
    assert extract_json('Aquí va: {"a": 1} fin') == {"a": 1}


def test_health():
    h = SourceHealth("wallapop")
    assert h.fail("network", "x") is None and h.fail("network", "x") is None
    assert "Aviso de desarrollador" in h.fail("network", "x")
    assert h.fail("network", "x") is None  # warns once
    assert "vuelve" in h.ok(5)
    assert "formato" in SourceHealth("w").fail("schema", "x")  # schema warns at once


class FakeSource:
    name = "fake"

    def __init__(self, items):
        self.items = items

    async def search(self, cfg):
        return self.items


class FakeOut:
    def __init__(self):
        self.listings, self.texts = [], []

    async def listing(self, alert_id, cfg, it, verdict, score, why, old_price=None):
        self.listings.append((it.id, verdict, old_price))

    async def text(self, text, alert_id=None):
        self.texts.append(text)


def test_process(monkeypatch):
    calls = []

    async def fake_judge(claude, c, items, aid):
        calls.append([i.id for i in items])
        return {i.id: {"verdict": "match" if i.id == "a" else "reject", "score": 7, "why": "w"} for i in items}

    monkeypatch.setattr("wallmpop.scheduler.judge_batch", fake_judge)
    db, out = DB(":memory:"), FakeOut()
    c = cfg()
    aid = db.add_alert(c, 1)
    src = FakeSource([item("a"), item("b", title="otro 512gb"), item("c", price=99)])
    s = Scheduler(db, src, None, out)
    asyncio.run(s.run_alert(aid, c))
    assert calls == [["a", "b"]] and out.listings == [("a", "match", None)]
    # second run: nothing new is judged; a price drop on a match re-notifies without LLM
    src.items = [item("a", price=30), item("b"), item("c", price=99)]
    asyncio.run(s.run_alert(aid, c))
    assert len(calls) == 1 and out.listings[-1] == ("a", "match", 40)
    # repost of "a" with a new id inherits the verdict, no LLM
    src.items = [item("a2", price=30)]
    asyncio.run(s.run_alert(aid, c))
    assert len(calls) == 1 and db.listing(aid, "a2")["verdict"] == "match"


def test_cap(monkeypatch):
    async def boom(*a):
        raise AssertionError("must not call the LLM")

    monkeypatch.setattr("wallmpop.scheduler.judge_batch", boom)
    db, out = DB(":memory:"), FakeOut()
    c = cfg(max_llm_per_day=1)
    aid = db.add_alert(c, 1)
    db.add_usage("haiku", aid, 1, 1, 0)
    s = Scheduler(db, FakeSource([item("a")]), None, out)
    asyncio.run(s.run_alert(aid, c))
    asyncio.run(s.run_alert(aid, c))
    assert len(out.texts) == 1 and "tope" in out.texts[0]


def test_users_and_owners():
    db = DB(":memory:")
    a1, a2 = db.add_alert(cfg(name="mine"), 1), db.add_alert(cfg(name="theirs"), 2)
    assert [r["id"] for r in db.alerts(owner=1)] == [a1]
    assert db.find_alert(str(a2), owner=1) is None and db.find_alert("theirs", owner=2)["id"] == a2
    db.set_user(2, "pending", "Ana", "ana")
    db.set_user(2, "allowed")
    assert db.user(2)["status"] == "allowed" and db.user(2)["name"] == "Ana"
    db.mute_seller(2, "s1")
    assert db.muted_sellers(2) == {"s1"} and db.muted_sellers(1) == set()
    db.pause_owner(2)
    assert db.alert(a2)["paused"] == 1 and db.alert(a1)["paused"] == 0


def test_gate():
    from types import SimpleNamespace as NS

    from telegram.ext import ApplicationHandlerStop

    from wallmpop.bot import Bot
    from wallmpop.config import Settings

    sent, replies = [], []

    class FakeBot:
        async def send_message(self, uid, text, **kw):
            sent.append((uid, text))

    async def reply(text, **kw):
        replies.append(text)

    b = Bot(Settings(telegram_token="1:x", allowed_user_ids={1}), DB(":memory:"), None)
    b.app = NS(bot=FakeBot())

    def upd(uid):
        return NS(effective_user=NS(id=uid, full_name="Eve", username="eve"), callback_query=None,
                  effective_message=NS(text="/start", reply_text=reply))

    asyncio.run(b.gate(upd(1), None))  # admin passes
    for _ in range(2):
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(b.gate(upd(7), None))
    assert len(sent) == 1 and sent[0][0] == 1 and "quiere usar" in sent[0][1]  # admin asked once
    assert "pendiente" in replies[-1]
    b.db.set_user(7, "allowed")
    asyncio.run(b.gate(upd(7), None))  # now passes
