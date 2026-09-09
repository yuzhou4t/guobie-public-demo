import importlib

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import get_db
from app.models import Document, ResearchCase
from app.services.public_demo_samples import sample_manifest


@pytest.fixture
def public_app(monkeypatch, session_factory):
    for key, value in {
        "PUBLIC_DEMO_ENABLED": "true",
        "AGENT_RUNTIME": "user_api",
        "PUBLIC_DEMO_SECRET_KEY": Fernet.generate_key().decode(),
        "PUBLIC_DEMO_ALLOWED_ORIGINS": "http://testserver",
        "LIVE_SEARCH_PROVIDER": "disabled",
        "PUBLIC_API_READ_ONLY": "false",
    }.items():
        monkeypatch.setenv("GUOBIE_" + key, value)
    get_settings.cache_clear()
    from app.cli.init_public_demo import initialize

    with session_factory() as db:
        initialize(db)
    module = importlib.import_module("app.public_demo")
    monkeypatch.setattr(module, "get_session_factory", lambda: session_factory)
    app = module.create_app()

    def database():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    yield app
    get_settings.cache_clear()


def test_original_reader_and_isolation(public_app, session_factory):
    with TestClient(public_app) as a, TestClient(public_app) as b:
        html = a.get("/reader/")
        assert html.status_code == 200
        assert "public-reader.js" in html.text
        assert a.get("/api/v1/reader/auth/status").json()["public_demo"]
        assert b.get("/api/v1/reader/auth/status").status_code == 200
        pa = a.get("/api/v1/reader/research-cases")
        pb = b.get("/api/v1/reader/research-cases")
        assert pa.status_code == pb.status_code == 200, (pa.text, pb.text)
        with session_factory() as db:
            cases = db.scalars(select(ResearchCase).order_by(ResearchCase.id)).all()
            assert len(cases) == 2
            assert cases[0].owner_id != cases[1].owner_id
            aid, bid = cases[0].id, cases[1].id
            assert db.scalar(select(func.count()).select_from(Document)) == len(
                sample_manifest()["documents"]
            )
        assert a.get(f"/api/v1/reader/research-cases/{bid}").status_code in {403, 404}
        assert b.get(f"/api/v1/reader/research-cases/{aid}").status_code in {403, 404}
        assert a.delete("/api/v1/reader/demo-session").status_code == 403
        headers = {"Origin": "http://testserver", "x-csrf-token": a.cookies.get("guobie_csrf")}
        cleared = a.delete("/api/v1/reader/demo-session", headers=headers)
        assert cleared.status_code == 200, cleared.text
        with session_factory() as db:
            assert db.get(ResearchCase, bid)
            assert not db.get(ResearchCase, aid)
            assert db.scalar(select(func.count()).select_from(Document)) == len(
                sample_manifest()["documents"]
            )


def test_model_connection_and_boundary(public_app, monkeypatch):
    from app.services.public_demo_runtime import UserAPIRuntime

    monkeypatch.setattr(UserAPIRuntime, "invoke", lambda self, **kwargs: {"ok": True})
    with TestClient(public_app) as a, TestClient(public_app) as b:
        a.get("/api/v1/reader/auth/status")
        b.get("/api/v1/reader/auth/status")
        headers = {"Origin": "http://testserver", "x-csrf-token": a.cookies.get("guobie_csrf")}
        payload = {
            "protocol": "chat_completions",
            "base_url": "https://api.deepseek.com",
            "model": "test-model",
            "api_key": "test-secret-value",
            "consent": True,
        }
        result = a.post("/api/v1/reader/model-connection/test", json=payload, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["connected"]
        assert "test-secret-value" not in result.text
        assert not b.get("/api/v1/reader/model-connection").json()["connected"]
        for path in [
            "/api/v1/reader/auth/bootstrap",
            "/api/v1/reader/field-library/1/publication",
            "/api/v1/reader/skill-workflows/materials",
            "/api/v1/sources",
        ]:
            assert a.post(path, json={}, headers=headers).status_code == 403
        assert a.delete("/api/v1/reader/model-connection", headers=headers).status_code == 200
        assert not a.get("/api/v1/reader/model-connection").json()["connected"]


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_original_runtime_contract(protocol):
    from app.services.public_demo_runtime import UserAPIRuntime

    class Provider:
        def post_json(self, endpoint, *, api_key, payload):
            assert api_key == "test-key"
            if protocol == "responses":
                assert endpoint.endswith("/responses") and payload["store"] is False
                return {
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}
                    ]
                }
            assert endpoint.endswith("/chat/completions")
            assert payload["thinking"] == {"type": "disabled"}
            return {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}

    runtime = UserAPIRuntime(
        sid="test",
        connection={"base_url": "https://api.deepseek.com", "model": "test", "protocol": protocol},
        api_key="test-key",
        client=Provider(),
    )
    assert runtime.invoke(
        instructions="test", prompt="test", schema={"type": "object", "required": ["ok"]}
    ) == {"ok": True}


def test_sample_initialization_rolls_back_all_batches(monkeypatch, session_factory):
    from app.cli import init_public_demo
    from app.models import Source

    monkeypatch.setenv("GUOBIE_PUBLIC_DEMO_ENABLED", "true")
    get_settings.cache_clear()

    def fail_after_documents(*args, **kwargs):
        raise RuntimeError("simulated interrupted initialization")

    monkeypatch.setattr(init_public_demo, "seed_capability_templates", fail_after_documents)
    with session_factory() as db:
        with pytest.raises(RuntimeError, match="interrupted"):
            init_public_demo.initialize(db)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 0
        assert db.scalar(select(func.count()).select_from(Document)) == 0
    get_settings.cache_clear()


def test_public_assistant_persists_user_api_run(public_app, session_factory, monkeypatch):
    from app.models import AgentRun
    from app.services import public_demo_runtime

    monkeypatch.setattr(public_demo_runtime, "get_session_factory", lambda: session_factory)
    with TestClient(public_app) as client:
        client.get("/api/v1/reader/auth/status")
        response = client.post(
            "/api/v1/reader/assistant/ask",
            json={
                "context": {"space": "country", "country_iso3": "COD"},
                "question": "核对刚果金资料来源",
                "online_mode": "off",
            },
            headers={"Origin": "http://testserver", "x-csrf-token": client.cookies.get("guobie_csrf")},
        )
        assert response.status_code == 503, response.text
        assert "请先在设置中连接" in response.json()["detail"]
        with session_factory() as db:
            run = db.scalar(select(AgentRun))
            assert run is not None
            assert run.runtime == "user_api"
            assert run.status == "failed"
            assert "连接" in run.error_message


def test_sample_upgrade_preserves_guests_history_and_is_idempotent(public_app, session_factory, monkeypatch):
    import copy

    from app.cli import init_public_demo
    from app.models import DocumentVersion, EventMention, ResearchEvent, StructuredSnapshot
    from app.models.public_demo import PublicDemoInstallation

    original = init_public_demo.sample_manifest()
    expanded = copy.deepcopy(original)
    expanded["previous_versions"] = [original["version"]]
    expanded["version"] = original["version"] + "-test-extension"
    expanded["documents"][0]["source_metadata"] = {"published_at_provenance": {"precision": "year"}}
    added = copy.deepcopy(expanded["documents"][0])
    added.update(
        title="Test-only public metadata", canonical_url="https://example.org/test-only-sample", doi=None
    )
    expanded["documents"].append(added)
    expanded["events"] = [
        {
            "event_key": "test-only-event",
            "series_key": "test-only-series",
            "title": "Test event",
            "event_type": "other",
            "date_precision": "year",
            "start_at": "2024-01-01T00:00:00+00:00",
            "end_at": None,
            "summary": "Test summary",
            "mentions": [{"document_url": added["canonical_url"]}],
        }
    ]
    with TestClient(public_app) as client:
        client.get("/api/v1/reader/auth/status")
        case_id = client.get("/api/v1/reader/research-cases").json()[0]["id"]
        with session_factory() as db:
            old_version_id = db.scalar(select(DocumentVersion.id).order_by(DocumentVersion.id))
            old_snapshots = set(db.scalars(select(StructuredSnapshot.id)))
            old_count = db.scalar(select(func.count()).select_from(Document))
        monkeypatch.setattr(init_public_demo, "sample_manifest", lambda: expanded)
        with session_factory() as db:
            init_public_demo.initialize(db)
        with session_factory() as db:
            assert db.get(ResearchCase, case_id)
            assert db.get(DocumentVersion, old_version_id)
            assert db.get(PublicDemoInstallation, 1).sample_version == expanded["version"]
            assert db.scalar(select(func.count()).select_from(Document)) == old_count + 1
            assert (
                db.scalar(select(func.count()).select_from(ResearchEvent))
                == len(original.get("events", [])) + 1
            )
            assert (
                db.scalar(select(func.count()).select_from(EventMention))
                == sum(len(e["mentions"]) for e in original.get("events", [])) + 1
            )
            assert old_snapshots < set(db.scalars(select(StructuredSnapshot.id)))
            version_count = db.scalar(select(func.count()).select_from(DocumentVersion))
            init_public_demo.initialize(db)
            assert db.scalar(select(func.count()).select_from(DocumentVersion)) == version_count
        assert client.get(f"/api/v1/reader/research-cases/{case_id}").status_code == 200


def test_public_country_sample_populates_sections_and_separates_news(public_app):
    with TestClient(public_app) as client:
        country = client.get("/api/v1/reader/countries/COD").json()
        assert len(country["events"]) == 8
        assert len(country["datasets"]) == 6
        policies = [item for item in country["policy_items"] if item["document_type"] == "policy_document"]
        assert len(policies) == 3
        news = client.get("/api/v1/reader/event-reports?country_iso3=COD&limit=100").json()
        assert len(news) == 49
        assert all(item["source_type"] == "news_media" for item in news)
        trends = client.get("/api/v1/reader/countries/COD/research-trends?years=5").json()
        domains = trends["frontier"]["taxonomy"]["domains"]
        assert len(domains) == 5
        assert all(item["count"] > 0 for item in domains)
        assert all(sum(year["count"] > 0 for year in item["years"]) >= 3 for item in domains)
