from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import requests
import zipfile

import graph_mail_downloader as graph


class StubResponse:
    def __init__(self, payload=None, status_code=200, headers=None, url="https://graph.microsoft.com/value", chunks=()) -> None:
        self.payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.chunks = chunks
        self.closed = False

    def json(self):
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size):
        yield from self.chunks


def test_inbox_collection_follows_next_link(monkeypatch) -> None:
    pages = {
        "https://graph.microsoft.com/v1.0/messages": StubResponse(
            {"value": [{"id": "first"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/messages?page=2"}
        ),
        "https://graph.microsoft.com/v1.0/messages?page=2": StubResponse(
            {"value": [{"id": "second"}]}
        ),
    }
    requested_urls = []

    def fake_get(url, **kwargs):
        requested_urls.append(url)
        return pages[url]

    monkeypatch.setattr(graph.requests, "get", fake_get)

    messages = graph._list_graph_collection("token", "https://graph.microsoft.com/v1.0/messages")

    assert [message["id"] for message in messages] == ["first", "second"]
    assert len(requested_urls) == 2


def test_attachment_listing_follows_next_link_and_requests_metadata_only(monkeypatch) -> None:
    first_url = graph._attachments_url("images@example.test", "message-id")
    second_url = "https://graph.microsoft.com/v1.0/attachments?page=2"
    pages = {
        first_url: StubResponse(
            {"value": [{"id": "first"}], "@odata.nextLink": second_url}
        ),
        second_url: StubResponse({"value": [{"id": "second"}]}),
    }
    monkeypatch.setattr(graph.requests, "get", lambda url, **kwargs: pages[url])

    attachments = graph._list_attachments("token", "images@example.test", "message-id")

    assert [attachment["id"] for attachment in attachments] == ["first", "second"]
    assert "$select=id,name,isInline,size" in first_url


def test_graph_pagination_rejects_external_next_link(monkeypatch) -> None:
    monkeypatch.setattr(
        graph.requests,
        "get",
        lambda *args, **kwargs: StubResponse(
            {"value": [], "@odata.nextLink": "https://graph.microsoft.com.attacker.test/page"}
        ),
    )

    try:
        graph._list_graph_collection("token", "https://graph.microsoft.com/v1.0/messages")
    except ValueError as exc:
        assert "host permitido" in str(exc)
    else:
        raise AssertionError("Se aceptó un nextLink fuera del host de Graph")


def test_graph_pagination_rejects_cycles_and_caps_pages(monkeypatch) -> None:
    first_url = "https://graph.microsoft.com/v1.0/messages"
    monkeypatch.setattr(
        graph.requests,
        "get",
        lambda url, **kwargs: StubResponse(
            {"value": [], "@odata.nextLink": first_url}
        ),
    )
    try:
        graph._list_graph_collection("token", first_url)
    except RuntimeError as exc:
        assert "ciclo" in str(exc)
    else:
        raise AssertionError("Se aceptó un ciclo de paginación")

    monkeypatch.setattr(graph, "GRAPH_MAX_PAGES", 1)
    monkeypatch.setattr(
        graph.requests,
        "get",
        lambda url, **kwargs: StubResponse(
            {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/messages?page=2"}
        ),
    )
    try:
        graph._list_graph_collection("token", first_url)
    except RuntimeError as exc:
        assert "máximo de páginas" in str(exc)
    else:
        raise AssertionError("Se excedió el máximo de páginas permitido")


def test_graph_collection_rejects_incomplete_or_malformed_pages(monkeypatch) -> None:
    for payload in ({}, {"value": None}, {"value": ["not-an-object"]}):
        monkeypatch.setattr(
            graph.requests,
            "get",
            lambda *args, _payload=payload, **kwargs: StubResponse(_payload),
        )
        try:
            graph._list_graph_collection(
                "token", "https://graph.microsoft.com/v1.0/messages"
            )
        except RuntimeError as exc:
            assert "colección" in str(exc)
        else:
            raise AssertionError(f"Se aceptó una página incompleta: {payload!r}")

    monkeypatch.setattr(
        graph.requests,
        "get",
        lambda *args, **kwargs: StubResponse(
            {
                "value": [],
                "@odata.nextLink": "",
            }
        ),
    )
    try:
        graph._list_graph_collection(
            "token", "https://graph.microsoft.com/v1.0/messages"
        )
    except RuntimeError as exc:
        assert "nextLink inválido" in str(exc)
    else:
        raise AssertionError("Se aceptó un nextLink vacío")


def test_graph_retries_429_using_retry_after(monkeypatch) -> None:
    responses = [
        StubResponse(status_code=429, headers={"Retry-After": "0"}),
        StubResponse({"ok": True}),
    ]
    delays = []
    monkeypatch.setattr(graph.requests, "get", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(graph, "_sleep_with_checkpoints", delays.append)

    response = graph._request_with_retries(graph.requests.get, "https://graph.microsoft.com/v1.0/me")

    assert response.json() == {"ok": True}
    assert delays == [0.0]


def test_graph_retry_logs_attempt_context_without_request_secrets(
    monkeypatch, caplog
) -> None:
    responses = [
        StubResponse(status_code=503),
        StubResponse({"ok": True}),
    ]
    monkeypatch.setattr(
        graph.requests, "get", lambda *args, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(graph, "_sleep_with_checkpoints", lambda _delay: None)

    graph._request_with_retries(
        graph.requests.get,
        "https://graph.microsoft.com/v1.0/messages?access_token=synthetic-secret",
        operation="attachment message_id=m-1 attachment_id=a-1",
    )

    assert "message_id=m-1 attachment_id=a-1" in caplog.text
    assert "attempt=1/4 status=503" in caplog.text
    assert "synthetic-secret" not in caplog.text


def test_graph_retries_are_bounded(monkeypatch) -> None:
    calls = []
    delays = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        return StubResponse(status_code=503)

    monkeypatch.setattr(graph.requests, "get", unavailable)
    monkeypatch.setattr(graph, "_sleep_with_checkpoints", delays.append)
    try:
        graph._request_with_retries(graph.requests.get, "https://graph.microsoft.com/v1.0/me")
    except requests.HTTPError:
        pass
    else:
        raise AssertionError("La respuesta 503 debía fallar después del límite")

    assert len(calls) == graph.GRAPH_MAX_ATTEMPTS
    assert len(delays) == graph.GRAPH_MAX_ATTEMPTS - 1
    assert delays == [1, 2, 4]


def test_send_alert_email_is_not_retried(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(graph, "_get_access_token", lambda config: "token")

    def reject_send(*args, **kwargs):
        calls.append(1)
        return StubResponse(status_code=503)

    monkeypatch.setattr(graph.requests, "post", reject_send)
    config = graph.M365Config("tenant", "client", "secret", "mailbox", graph.Path("."))
    try:
        graph.send_alert_email("subject", "body", "user@example.test", config)
    except graph.MailDeliveryAmbiguous:
        pass
    else:
        raise AssertionError("Un 5xx de sendMail debe tratarse como resultado ambiguo")

    assert len(calls) == 1


def test_download_link_parser_selects_images_and_ignores_reports() -> None:
    body = (
        '<p>Descargar las imágenes <a href="https://example.test/images.zip">ZIP</a></p>'
        '<p><a href="https://example.test/report.zip">Informe</a></p>'
    )

    assert graph._find_download_links(body) == ["https://example.test/images.zip"]


def test_state_write_is_atomic_when_replace_fails(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text('{"processed_message_ids": ["old"]}', encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr(graph.os, "replace", fail_replace)

    try:
        graph._save_state(state_file, {"processed_message_ids": ["new"]})
    except OSError:
        pass
    else:
        raise AssertionError("La escritura simulada debía fallar")

    assert graph._load_state(state_file) == {"processed_message_ids": ["old"]}
    assert list(tmp_path.iterdir()) == [state_file]


def test_mail_state_lock_prevents_lost_updates_across_processes(tmp_path) -> None:
    ready = tmp_path / "ready"
    ready.mkdir()
    release = tmp_path / "release"
    state_file = tmp_path / "state.json"
    script = """
import json
import sys
import time
from pathlib import Path
from filelock import FileLock
import graph_mail_downloader as graph

root = Path(sys.argv[1])
worker_id = sys.argv[2]
(root / "ready" / worker_id).touch()
release = root / "release"
deadline = time.monotonic() + 10
while not release.exists():
    if time.monotonic() > deadline:
        raise TimeoutError("parent did not release workers")
    time.sleep(0.01)
with FileLock(root / ".mail.lock", timeout=10):
    state_file = root / "state.json"
    state = graph._load_state(state_file) if state_file.exists() else {
        "processed_message_ids": []
    }
    state["processed_message_ids"].append(worker_id)
    time.sleep(0.1)
    graph._save_state(state_file, state)
"""
    project = Path(__file__).resolve().parents[1]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), worker_id],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker_id in ("message-a", "message-b")
    ]
    try:
        deadline = time.monotonic() + 15
        while len(list(ready.iterdir())) < 2 and time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                break
            time.sleep(0.02)
        assert len(list(ready.iterdir())) == 2
        release.touch()
        outputs = [process.communicate(timeout=15) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
    finally:
        release.touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(state["processed_message_ids"]) == {"message-a", "message-b"}


def test_corrupt_state_fails_closed(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("not-json", encoding="utf-8")

    try:
        graph._load_state(state_file)
    except RuntimeError as exc:
        assert "leer el estado" in str(exc)
    else:
        raise AssertionError("El estado corrupto se trató como estado vacío")


def test_mail_runs_are_serialized_with_local_state_lock(tmp_path, monkeypatch) -> None:
    import process_activity
    import repository_storage

    active = 0
    peak_active = 0
    guard = threading.Lock()
    root = tmp_path / "images"
    state = tmp_path / "state"

    @contextmanager
    def fake_record_process(_name):
        yield {}

    def fake_download(*args):
        nonlocal active, peak_active
        with guard:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return graph.DownloadSummary(0, 0, 0, [], [])

    monkeypatch.setattr(graph, "repository_root", lambda: root)
    monkeypatch.setattr(repository_storage, "state_root", lambda _root: state)
    monkeypatch.setattr(repository_storage, "check_image_root", lambda _root: None)
    monkeypatch.setattr(graph, "incoming_lock", lambda _root: nullcontext())
    monkeypatch.setattr(process_activity, "record_process", fake_record_process)
    monkeypatch.setattr(graph, "_download_luxoptica_mail_attachments", fake_download)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: graph.download_luxoptica_mail_attachments(), range(2)))

    assert len(results) == 2
    assert peak_active == 1


def test_zip_limits_reject_oversized_member_before_extract(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "large.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("model__color.png", b"12345")
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ENTRY_BYTES", "4")
    monkeypatch.setattr(graph, "_load_eans_by_image_key", lambda: {})
    monkeypatch.setattr(graph, "_get_market_ids_by_ean", lambda eans: {})

    try:
        graph._extract_zip(archive_path, tmp_path / "images")
    except ValueError as exc:
        assert "GRAPH_IMAGE_MAX_ENTRY_BYTES" in str(exc)
    else:
        raise AssertionError("Se extrajo un miembro superior al límite")


def test_zip_total_limit_covers_non_image_members(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "large-total.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("readme.txt", b"1234")
        archive.writestr("metadata.json", b"5678")
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ZIP_UNPACKED_BYTES", "5")
    monkeypatch.setattr(graph, "_load_eans_by_image_key", lambda: {})
    monkeypatch.setattr(graph, "_get_market_ids_by_ean", lambda eans: {})

    try:
        graph._extract_zip(archive_path, tmp_path / "images")
    except ValueError as exc:
        assert "GRAPH_IMAGE_MAX_ZIP_UNPACKED_BYTES" in str(exc)
    else:
        raise AssertionError("Se excedió el límite total de extracción")


def test_streamed_attachment_enforces_limit_without_publishing_partial_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(graph, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(graph, "work_root", lambda _root: tmp_path)
    monkeypatch.setattr(graph, "check_work_root", lambda _root: None)
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ARCHIVE_BYTES", "3")
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_RESERVE_BYTES", "1")

    try:
        graph._save_streamed_attachment([b"ab", b"cd"], tmp_path, "file.zip")
    except ValueError as exc:
        assert "GRAPH_IMAGE_MAX_ARCHIVE_BYTES" in str(exc)
    else:
        raise AssertionError("Se descargaron más bytes que el límite")

    assert not (tmp_path / "file.zip").exists()
    assert list(tmp_path.iterdir()) == []


def test_interrupted_attachment_is_not_published_and_can_be_retried(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(graph, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(graph, "work_root", lambda _root: tmp_path)
    monkeypatch.setattr(graph, "check_work_root", lambda _root: None)
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ARCHIVE_BYTES", "100")
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_RESERVE_BYTES", "1")

    def interrupted():
        yield b"partial"
        raise OSError("synthetic network interruption")

    try:
        graph._save_streamed_attachment(interrupted(), tmp_path, "resume.zip")
    except OSError as exc:
        assert "synthetic network interruption" in str(exc)
    else:
        raise AssertionError("La interrupción simulada debía propagarse")

    assert not (tmp_path / "resume.zip").exists()
    assert not list(tmp_path.glob("*.part"))
    recovered = graph._save_streamed_attachment(
        [b"complete", b"-archive"], tmp_path, "resume.zip"
    )
    repeated = graph._save_streamed_attachment(
        [b"complete", b"-archive"], tmp_path, "resume.zip"
    )
    assert recovered == repeated
    assert recovered.read_bytes() == b"complete-archive"
    assert list(tmp_path.glob("*.zip")) == [recovered]


def test_zip_link_download_uses_streaming_and_publishes_complete_file(tmp_path, monkeypatch) -> None:
    response = StubResponse(
        headers={"content-length": "4"},
        url="https://downloads.example.test/images.zip",
        chunks=[b"ab", b"cd"],
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(graph.requests, "get", fake_get)
    monkeypatch.setattr(graph, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(graph, "work_root", lambda _root: tmp_path)
    monkeypatch.setattr(graph, "check_work_root", lambda _root: None)
    monkeypatch.setenv("GRAPH_IMAGE_MAX_ARCHIVE_BYTES", "10")
    monkeypatch.setenv("IMAGE_REPOSITORY_WORK_RESERVE_BYTES", "1")

    saved = graph._download_zip_link(response.url, tmp_path)

    assert saved == tmp_path / "images.zip"
    assert saved.read_bytes() == b"abcd"
    assert calls[0]["stream"] is True
    assert response.closed