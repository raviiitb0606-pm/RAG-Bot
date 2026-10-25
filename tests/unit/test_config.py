from mf_faq.config import Settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.model == "openai/gpt-oss-120b"
    assert s.reasoning_effort == "low"
    assert s.retrieval_threshold == 0.69
    assert (s.retrieval_top_k, s.holdings_max_tokens) == (4, 400)
    assert s.embed_query_instruction.startswith("Represent this sentence")
    assert s.default_source_id == "groww-hdfc-large-cap"
    assert s.groq_api_key is None


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("MF_FAQ_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("MF_FAQ_HISTORY_TURNS", "4")
    s = Settings(_env_file=None)
    assert s.groq_api_key.get_secret_value() == "gsk_test"
    assert s.model == "llama-3.3-70b-versatile"
    assert s.history_turns == 4
    assert "gsk_test" not in repr(s)


def test_embedder_uses_the_pinned_revision(monkeypatch):
    """D2: every process embeds with the same model commit (an index only loads with its own)."""
    from mf_faq.ingest.index import embedder_from

    calls = []
    monkeypatch.setattr(
        "mf_faq.ingest.index.make_embedder", lambda name, **kw: calls.append((name, kw))
    )
    pinned = Settings(_env_file=None)
    assert pinned.embed_model_revision == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    embedder_from(pinned)
    embedder_from(Settings(_env_file=None, embed_model_revision=None))
    assert calls == [
        ("BAAI/bge-small-en-v1.5", {"revision": pinned.embed_model_revision}),
        ("BAAI/bge-small-en-v1.5", {}),
    ]
