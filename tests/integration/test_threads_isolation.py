"""DL-3, arch §8.3: parallel threads keep their own scheme and history."""

import asyncio

import httpx
from api_support import URL_BY_SCHEME, ScriptedGenerator, make_app

SCHEMES = {
    "hdfc-mid-cap": "HDFC Mid Cap Fund",
    "hdfc-small-cap": "HDFC Small Cap Fund",
    "hdfc-multi-cap": "HDFC Multi Cap Fund",
}
FOLLOW_UPS = ("What about its exit load?", "And the expense ratio?")


async def converse(api: httpx.AsyncClient, scheme_name: str) -> tuple[str, list[dict]]:
    tid = (await api.post("/api/threads")).json()["id"]
    replies = []
    for text in (f"What is the expense ratio of {scheme_name}?", *FOLLOW_UPS):
        resp = await api.post(f"/api/threads/{tid}/messages", json={"text": text})
        assert resp.status_code == 200
        replies.append(resp.json())
        await asyncio.sleep(0)  # let the other conversations interleave
    return tid, replies


async def test_parallel_threads_follow_up_on_their_own_scheme(tmp_path):
    gen = ScriptedGenerator()
    app = make_app(tmp_path, gen)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        results = await asyncio.gather(*(converse(api, name) for name in SCHEMES.values()))
        for (scheme_id, name), (tid, replies) in zip(SCHEMES.items(), results, strict=True):
            assert [r["kind"] for r in replies] == ["answer"] * 3
            assert {r["source_url"] for r in replies} == {URL_BY_SCHEME[scheme_id]}, name
            history = (await api.get(f"/api/threads/{tid}/messages")).json()["messages"]
            questions = [m["text"] for m in history if m["role"] == "user"]
            assert questions == [f"What is the expense ratio of {name}?", *FOLLOW_UPS]
            assert app.state.service.store.get_thread(tid).active_scheme_id == scheme_id

    # What the model saw: each prompt's history names only its own thread's scheme.
    assert len(gen.calls) == 3 * len(SCHEMES)
    for messages in gen.calls:
        history = " ".join(m["content"] for m in messages[1:-1] if m["role"] == "user")
        named = {name for name in SCHEMES.values() if name in history}
        assert len(named) <= 1, named
        if named:
            (name,) = named
            scheme_id = next(k for k, v in SCHEMES.items() if v == name)
            assert f'source="{_source_id(scheme_id)}"' in messages[-1]["content"]


def _source_id(scheme_id: str) -> str:
    return "groww-" + scheme_id


async def test_follow_up_in_a_new_thread_does_not_inherit_a_scheme(tmp_path):
    app = make_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        a = (await api.post("/api/threads")).json()["id"]
        b = (await api.post("/api/threads")).json()["id"]
        await api.post(f"/api/threads/{a}/messages", json={"text": "Exit load of HDFC Mid Cap?"})
        reply = (
            await api.post(f"/api/threads/{b}/messages", json={"text": "What about its exit load?"})
        ).json()
    assert reply["kind"] == "clarify"
