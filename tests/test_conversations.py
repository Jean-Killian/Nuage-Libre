"""Integration tests for the /api/conversations routes and /api/health."""

import re
import uuid

from tests.conftest import parse_events


async def create_conversation(client, message: str = "Hi") -> str:
    events = parse_events(await client.post("/api/chat", json={"message": message}))
    return events[0]["id"]


async def test_list_is_empty_at_start(client):
    """T-CONV-01: no conversation exists on a fresh database."""
    response = await client.get("/api/conversations")

    assert response.status_code == 200
    assert response.json() == []


async def test_list_orders_most_recent_first(client):
    """T-CONV-02: the conversation updated last appears first in the sidebar."""
    first = await create_conversation(client, "first")
    second = await create_conversation(client, "second")
    # Writing in the first conversation moves it back to the top.
    await client.post("/api/chat", json={"conversation_id": first, "message": "again"})

    ids = [c["id"] for c in (await client.get("/api/conversations")).json()]

    assert ids == [first, second]


async def test_list_limit_is_validated(client):
    """T-CONV-03: ?limit= must be between 1 and 200 (protects the server)."""
    assert (await client.get("/api/conversations?limit=0")).status_code == 422
    assert (await client.get("/api/conversations?limit=201")).status_code == 422
    assert (await client.get("/api/conversations?limit=1")).status_code == 200


async def test_get_unknown_conversation_returns_404(client):
    """T-CONV-04"""
    response = await client.get(f"/api/conversations/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_delete_removes_conversation_and_messages(client):
    """T-CONV-05: deleting a conversation also deletes its messages (ON DELETE CASCADE)."""
    conversation_id = await create_conversation(client)

    response = await client.delete(f"/api/conversations/{conversation_id}")

    assert response.status_code == 204
    assert (await client.get(f"/api/conversations/{conversation_id}")).status_code == 404
    # A second delete of the same id is reported as not found.
    assert (await client.delete(f"/api/conversations/{conversation_id}")).status_code == 404


async def test_health_reports_services(client):
    """T-HEALTH-01: the health route reports the database and AI engine status."""
    body = (await client.get("/api/health")).json()

    assert body["database"] == "up"
    assert body["ollama"] == "up"
    assert body["ollama_version"] == "fake-1.0"


async def test_index_page_is_served(client):
    """T-UI-01: the web interface is served at the root URL."""
    response = await client.get("/")

    assert response.status_code == 200
    assert "Nuage Libre" in response.text


async def test_front_end_assets_are_served(client):
    """T-UI-02: every stylesheet, script and font referenced by the page is
    served by FastAPI's /static mount with the right content type."""
    page = (await client.get("/")).text
    assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)

    # The page really is split into separate files (themes, layout, logic...).
    assert "/static/css/themes.css" in assets
    assert "/static/js/app.js" in assets
    for path in assets:
        response = await client.get(path)
        assert response.status_code == 200, path
        if path.endswith(".css"):
            assert response.headers["content-type"].startswith("text/css"), path
        elif path.endswith(".js"):
            assert "javascript" in response.headers["content-type"], path


async def test_interface_files_are_revalidated(client):
    """T-UI-03: the page and its assets carry "Cache-Control: no-cache" and an
    ETag, so a browser never mixes an old stylesheet with a new script after
    an update, and an unchanged file only costs a "304 Not Modified"."""
    page = await client.get("/")
    css = await client.get("/static/css/app.css")
    unchanged = await client.get("/static/css/app.css", headers={"If-None-Match": css.headers["etag"]})

    assert page.headers["cache-control"] == "no-cache"
    assert css.headers["cache-control"] == "no-cache"
    assert unchanged.status_code == 304


async def test_page_loads_nothing_from_the_internet(client):
    """T-SEC-04 (sovereignty): the UI references no third-party server, so
    opening it never leaks the user's IP address or activity to a CDN.
    Checked on the HTML page and on every stylesheet it links (our own CSS,
    and vendored CSS such as KaTeX's, which loads fonts with url(...))."""
    page = (await client.get("/")).text
    css = ""
    for path in re.findall(r'href="(/static/[^"]+\.css)"', page):
        css += (await client.get(path)).text

    # Any absolute (http://, https://) or protocol-relative (//) URL is external.
    external = re.findall(r'(?:src|href)="((?:https?:)?//[^"]*)"', page)
    external += re.findall(r"url\(\s*['\"]?((?:https?:)?//[^)'\"]*)", css)
    assert external == []
