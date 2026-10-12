from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import base64
import json
import os
import re
import zipfile
from typing import Any
from collections.abc import Iterable
from urllib.parse import unquote, urlparse

import requests
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL

from db_config import load_db_config, load_env_file
from filelock import FileLock
from image_repository import ImageRepository, atomic_write, collision_name, file_checksum, repository_root
import hashlib
from image_naming import sanitize_filename as _sanitize_filename
from image_naming import market_view_from_image_name as _market_view_from_image_name
from image_work_storage import work_root, check_work_root, require_capacity, archive_limit, incoming_lock, positive_setting
from service_stop import checkpoint, request_timeout, acquire, database_options


M365_TENANT_ID_ENV = "M365_TENANT_ID"
M365_CLIENT_ID_ENV = "M365_CLIENT_ID"
M365_CLIENT_SECRET_ENV = "M365_CLIENT_SECRET"
M365_MAILBOX_ENV = "M365_MAILBOX"
M365_DOWNLOAD_ROOT_ENV = "M365_DOWNLOAD_ROOT"


@dataclass
class M365Config:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str
    download_root: Path


@dataclass
class DownloadSummary:
    messages_scanned: int
    messages_with_attachments: int
    attachments_downloaded: int
    saved_paths: list[str]
    processed_message_ids: list[str]


def load_m365_config() -> M365Config:
    load_env_file()
    return M365Config(
        tenant_id=os.getenv(M365_TENANT_ID_ENV, "").strip(),
        client_id=os.getenv(M365_CLIENT_ID_ENV, "").strip(),
        client_secret=os.getenv(M365_CLIENT_SECRET_ENV, "").strip(),
        mailbox=os.getenv(M365_MAILBOX_ENV, "images@diagonaleyewear.com").strip(),
        download_root=repository_root(),
    )


def validate_m365_config(config: M365Config) -> list[str]:
    missing: list[str] = []
    if not config.tenant_id:
        missing.append(M365_TENANT_ID_ENV)
    if not config.client_id:
        missing.append(M365_CLIENT_ID_ENV)
    if not config.client_secret:
        missing.append(M365_CLIENT_SECRET_ENV)
    if not config.mailbox:
        missing.append(M365_MAILBOX_ENV)
    return missing


def _token_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def _graph_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get_access_token(config: M365Config) -> str:
    checkpoint()
    with closing(requests.post(
        _token_url(config.tenant_id),
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=request_timeout(30),
    )) as response:
        response.raise_for_status()
        payload = response.json()
    token = payload.get("access_token", "")
    if not token:
        raise RuntimeError("No se obtuvo access_token de Microsoft Graph")
    return token


def _inbox_messages_url(mailbox: str, top: int) -> str:
    select_fields = "id,subject,from,receivedDateTime,hasAttachments,isRead"
    return (
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/inbox/messages"
        f"?$select={select_fields}&$orderby=receivedDateTime desc&$top={top}"
    )


def _list_inbox_messages(token: str, mailbox: str, top: int) -> list[dict[str, Any]]:
    checkpoint()
    with closing(requests.get(_inbox_messages_url(mailbox, top), headers=_graph_headers(token),
                              timeout=request_timeout(30))) as response:
        response.raise_for_status()
        payload = response.json()
    return payload.get("value", [])


def _attachments_url(mailbox: str, message_id: str) -> str:
    return f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments?$top=200"


def _attachment_value_url(mailbox: str, message_id: str, attachment_id: str) -> str:
    return f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value"


def _message_body_url(mailbox: str, message_id: str) -> str:
    return f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}?$select=body"


def _list_attachments(token: str, mailbox: str, message_id: str) -> list[dict[str, Any]]:
    checkpoint()
    with closing(requests.get(_attachments_url(mailbox, message_id), headers=_graph_headers(token),
                              timeout=request_timeout(30))) as response:
        response.raise_for_status()
        payload = response.json()
    return payload.get("value", [])


def _get_message_body(token: str, mailbox: str, message_id: str) -> str:
    checkpoint()
    with closing(requests.get(_message_body_url(mailbox, message_id), headers=_graph_headers(token),
                              timeout=request_timeout(30))) as response:
        response.raise_for_status()
        return response.json().get("body", {}).get("content", "")


def send_alert_email(
    subject: str,
    html_body: str,
    to_address: str | list[str],
    config: M365Config | None = None,
) -> None:
    """Envia un email desde el buzon configurado (requiere permiso Mail.Send)."""
    config = config or load_m365_config()
    missing = validate_m365_config(config)
    if missing:
        raise RuntimeError(f"Faltan variables de configuracion M365: {', '.join(missing)}")

    direcciones = [to_address] if isinstance(to_address, str) else to_address
    token = _get_access_token(config)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": direccion}} for direccion in direcciones],
        },
        "saveToSentItems": True,
    }
    with closing(requests.post(
        f"https://graph.microsoft.com/v1.0/users/{config.mailbox}/sendMail",
        headers={**_graph_headers(token), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )) as response:
        response.raise_for_status()


def _mark_message_read(token: str, mailbox: str, message_id: str) -> None:
    with closing(requests.patch(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages/{message_id}",
        headers={**_graph_headers(token), "Content-Type": "application/json"},
        json={"isRead": True},
        timeout=request_timeout(30),
    )) as response:
        response.raise_for_status()


def _find_download_links(body: str) -> list[str]:
    import re
    from html import unescape

    html = unescape(body or "")
    links: list[tuple[str, str, str]] = []
    anchor_pattern = re.compile(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>.*?</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_pattern.finditer(html):
        before = re.sub(r"<[^>]+>", " ", html[max(0, match.start() - 350) : match.start()]).lower()
        after = re.sub(r"<[^>]+>", " ", html[match.end() : match.end() + 140]).lower()
        links.append((match.group(1), before, after))

    image_links = [
        href
        for href, before, after in links
        if ("descargar las imágenes" in before or "download the images" in before)
        and "informe" not in after
        and "report" not in after
    ]
    if image_links:
        return image_links
    return [href for href, _, _ in links]


def _load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {"processed_message_ids": []}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"No se puede leer el estado {state_file}") from error


def _save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(state_file, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))
    if os.name == "posix":
        directory = os.open(state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _parse_graph_dt(value: str) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _detect_lote_from_subject(subject: str) -> str:
    text = (subject or "").lower()
    marker = "lote"
    if marker not in text:
        return "lote-unknown"
    idx = text.find(marker)
    tail = text[idx : idx + 20]
    digits = "".join(ch for ch in tail if ch.isdigit())
    if not digits:
        return "lote-unknown"
    return f"lote-{int(digits):03d}"


def _save_attachment_bytes(content: bytes, root: Path, received_at: datetime, lote: str, file_name: str) -> Path:
    from repository_storage import check_image_root
    check_image_root(root)
    date_folder = received_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    incoming = work_root(root)
    check_work_root(incoming)
    incoming.mkdir(parents=True, exist_ok=True)
    if len(content) > archive_limit():
        raise ValueError("Adjunto supera GRAPH_IMAGE_MAX_ARCHIVE_BYTES")
    require_capacity(incoming, len(content))
    target_dir = incoming / date_folder / lote
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / _sanitize_filename(file_name)
    checksum = hashlib.sha256(content).hexdigest()
    if out_path.exists() and hashlib.sha256(out_path.read_bytes()).hexdigest() != checksum:
        out_path = target_dir / collision_name(out_path.name, checksum)
    if out_path.exists() and hashlib.sha256(out_path.read_bytes()).hexdigest() != checksum:
        raise ValueError(f"Conflicto en archivo recibido: {out_path}")
    if not out_path.exists():
        atomic_write(out_path, content, guard=lambda: check_work_root(incoming))
    return out_path


def _model_from_image_name(file_name: str) -> str:
    model = Path(file_name).name.split("__", 1)[0].strip()
    if model.startswith("0"):
        model = model[1:]
    return _sanitize_filename(model or "modelo-unknown")


def _normalize_image_key(value: str, remove_initial_zero: bool = False) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if remove_initial_zero and normalized.startswith("0"):
        normalized = normalized[1:]
    return normalized


def _image_model_color_key(file_name: str) -> tuple[str, str] | None:
    parts = Path(file_name).stem.split("__")
    if len(parts) < 2:
        return None
    model = _normalize_image_key(parts[0], remove_initial_zero=True)
    color_part = re.sub(r"_\d{3}A$", "", parts[1], flags=re.IGNORECASE)
    color = _normalize_image_key(color_part)
    if not model or not color:
        return None
    return model, color


def _load_eans_by_image_key() -> dict[tuple[str, str], str]:
    """Carga la relacion modelo/color/EAN de los manifiestos de solicitudes."""
    manifest_root = Path(__file__).resolve().parent / "docs" / "Luxoptica"
    matches: dict[tuple[str, str], set[str]] = {}
    for manifest_path in manifest_root.glob("upc-products-images-request-*.manifest.json"):
        try:
            products = json.loads(manifest_path.read_text(encoding="utf-8")).get("products", [])
        except (OSError, json.JSONDecodeError):
            continue
        for product in products:
            key = (
                _normalize_image_key(product.get("modelo", ""), remove_initial_zero=True),
                _normalize_image_key(product.get("color", "")),
            )
            ean = str(product.get("ean", "")).strip()
            if key[0] and key[1] and ean:
                matches.setdefault(key, set()).add(ean)
    return {key: next(iter(eans)) for key, eans in matches.items() if len(eans) == 1}


def _get_market_ids_by_ean(eans: set[str]) -> dict[str, dict[str, str]]:
    """Obtiene los IDs externos de Farfetch y Miinto para los EAN indicados."""
    if not eans:
        return {}

    config = load_db_config()
    if not all([config.host, config.database, config.user]):
        print("   No se pudo consultar Odoo: falta configuracion de base de datos.", flush=True)
        return {}

    engine = create_engine(
        URL.create(
            "postgresql+psycopg2",
            username=config.user,
            password=config.password,
            host=config.host,
            port=config.port,
            database=config.database,
        ),
        connect_args=database_options(),
    )
    query = text(
        """
        SELECT pp.barcode AS ean, dpw.siteweb_id, dpw.product_website_id
        FROM product_product pp
        JOIN diagonal_product_website dpw ON dpw.product_id = pp.id
        WHERE pp.barcode IN :eans
          AND dpw.siteweb_id IN (2, 3)
          AND COALESCE(dpw.product_website_id, '') <> ''
        """
    ).bindparams(bindparam("eans", expanding=True))
    market_names = {2: "Miinto", 3: "Farfetch"}
    result: dict[str, dict[str, str]] = {}
    checkpoint()
    try:
        with engine.connect() as conn:
            for row in conn.execute(query, {"eans": sorted(eans)}).mappings():
                result.setdefault(row["ean"], {})[market_names[row["siteweb_id"]]] = row["product_website_id"]
    finally:
        engine.dispose()
    return result


def _market_image_names(file_name: str, market_ids: dict[str, str]) -> dict[str, str]:
    view = _market_view_from_image_name(file_name)
    if view is None or Path(file_name).suffix.lower() != ".png":
        return {}
    names = {}
    if market_ids.get("Farfetch"):
        names["Farfetch"] = f"{market_ids['Farfetch']}_{view}.png"
    if market_ids.get("Miinto"):
        names["Miinto"] = f"{market_ids['Miinto']}_{view}.jpeg"
    return names


def _create_market_images(source: Path, ean_dir: Path, market_ids: dict[str, str]) -> list[Path]:
    repository = ImageRepository(ean_dir.parent)
    records = [record for record in repository.records(ean_dir.name)
               if record.path == source and not record.market and _market_image_names(record.name, market_ids)]
    created: list[Path] = []
    for original in records:
        for market, name in _market_image_names(original.name, market_ids).items():
            if any(record.name == name and record.market == market and record.checksum == original.checksum
                   and record.provider == original.provider and record.origin == original.origin
                   for record in repository.records(original.ean)):
                continue
            record, _ = repository.save(
                original.ean, name, repository.verified_bytes(original), provider=original.provider,
                origin=original.origin, view=original.view, market=market, metadata=original.metadata,
                date=original.date, allow_invalid=True,
            )
            created.append(record.path)
    return created


def _market_pending_file(images_root: Path) -> Path:
    from repository_storage import state_root
    return state_root(images_root) / ".market_pending.json"


def _refresh_pending_market_images(images_root: Path) -> tuple[int, int]:
    from repository_storage import check_image_root
    check_image_root(images_root)
    checkpoint()
    with acquire(FileLock(images_root / ".market.lock", timeout=120)):
        return _refresh_pending_market_images_locked(images_root)


def _refresh_pending_market_images_locked(images_root: Path) -> tuple[int, int]:
    """Reintenta registrar variantes sin duplicar los bytes de los originales."""
    pending_file = _market_pending_file(images_root)
    repository = ImageRepository(images_root)
    pending_entries: dict[str, dict[str, Any]] = {}
    for record in repository.records():
        checkpoint()
        if record.market or not _market_image_names(record.name, {"Farfetch": "check"}):
            continue
        pending_entries[str(record.path)] = {"ean": record.ean, "missing_markets": ["Farfetch", "Miinto"]}

    market_ids_by_ean = _get_market_ids_by_ean({entry["ean"] for entry in pending_entries.values()})
    completed = 0
    created = 0
    for source_name, entry in list(pending_entries.items()):
        checkpoint()
        source = Path(source_name)
        market_ids = market_ids_by_ean.get(entry["ean"], {})
        created_paths = _create_market_images(source, source.parent, market_ids)
        created += len(created_paths)
        registered = {record.market for record in repository.records(entry["ean"]) if record.path == source}
        missing = [market for market in ("Farfetch", "Miinto") if market not in registered]
        if missing:
            entry["missing_markets"] = missing
            pending_entries[source_name] = entry
        else:
            completed += 1
            pending_entries.pop(source_name, None)

    if pending_entries:
        _save_state(pending_file, pending_entries)
    elif pending_file.exists():
        pending_file.unlink()

    return completed, created


def _extract_zip(archive_path: Path, images_root: Path) -> list[Path]:
    extracted_paths: list[Path] = []
    repository = ImageRepository(images_root)
    eans_by_image_key = _load_eans_by_image_key()
    matched_eans = set(eans_by_image_key.values())
    market_ids_by_ean = _get_market_ids_by_ean(matched_eans)

    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > positive_setting("GRAPH_IMAGE_MAX_ZIP_ENTRIES", 50000):
            raise ValueError("ZIP supera GRAPH_IMAGE_MAX_ZIP_ENTRIES; archivo conservado")
        for member in archive.infolist():
            checkpoint()
            if member.is_dir():
                continue

            file_name = _sanitize_filename(Path(member.filename.replace("\\", "/")).name)
            if Path(file_name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            if member.file_size > positive_setting("GRAPH_IMAGE_MAX_ENTRY_BYTES", 30 * 1024**2):
                raise ValueError("Imagen ZIP supera GRAPH_IMAGE_MAX_ENTRY_BYTES; archivo conservado")
            image_key = _image_model_color_key(file_name)
            ean = eans_by_image_key.get(image_key) if image_key else None
            if ean is None:
                raise ValueError(f"EAN no identificado para {file_name}; ZIP conservado en {archive_path}")
            view = _market_view_from_image_name(file_name) or "unknown"
            record, _ = repository.save(
                ean, file_name, archive.read(member), provider="Luxoptica",
                origin=f"ZIP:{archive_path.name}:{member.filename}", view=view,
                metadata={"modelo": _model_from_image_name(file_name)}, allow_invalid=True,
            )
            extracted_paths.append(record.path)
            market_ids = market_ids_by_ean.get(ean, {})
            extracted_paths.extend(_create_market_images(record.path, record.path.parent, market_ids))
    _refresh_pending_market_images(repository.root)
    return extracted_paths


def _save_streamed_attachment(chunks: Iterable[bytes], target_dir: Path, file_name: str) -> Path:
    incoming = work_root(repository_root())
    check_work_root(incoming)
    if incoming != target_dir and incoming not in target_dir.parents:
        raise ValueError("Destino de adjunto fuera de temporales")
    require_capacity(incoming, archive_limit())
    target_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target_dir, suffix=".part")
    staged = Path(temporary)
    try:
        total = 0
        with os.fdopen(descriptor, "wb") as stream:
            for chunk in chunks:
                checkpoint()
                total += len(chunk)
                if total > archive_limit():
                    raise ValueError("Adjunto supera GRAPH_IMAGE_MAX_ARCHIVE_BYTES")
                require_capacity(incoming, len(chunk))
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint()
        saved = target_dir / _sanitize_filename(file_name)
        digest = file_checksum(staged)
        if saved.exists() and file_checksum(saved) != digest:
            saved = target_dir / collision_name(saved.name, digest)
        if saved.exists():
            if file_checksum(saved) != digest:
                raise ValueError("Conflicto en adjunto recibido")
        else:
            check_work_root(incoming)
            staged.replace(saved)
        return saved
    finally:
        check_work_root(incoming)
        if staged.exists():
            staged.unlink()


def _download_zip_link(url: str, target_dir: Path) -> Path | None:
    checkpoint()
    with requests.get(url, allow_redirects=True, stream=True, timeout=request_timeout(120)) as response:
        response.raise_for_status()
        final_path = unquote(urlparse(response.url).path)
        if not final_path.lower().endswith(".zip"):
            return None

        file_name = Path(final_path).name or "luxoptica-images.zip"
        total_bytes = int(response.headers.get("content-length", "0") or 0)
        if total_bytes > archive_limit():
            raise ValueError("ZIP supera GRAPH_IMAGE_MAX_ARCHIVE_BYTES")
        print(f"   Descargando {file_name} ({total_bytes / 1024 / 1024:.1f} MB)...", flush=True)
        saved = _save_streamed_attachment(response.iter_content(1024 * 1024), target_dir, file_name)
        print("   ZIP descargado y verificado.", flush=True)
        return saved


def download_luxoptica_mail_attachments(
    sender_hint: str = "luxottica",
    subject_hint: str = "image",
    lookback_days: int = 7,
    top_messages: int = 100,
) -> DownloadSummary:
    root = repository_root()
    from repository_storage import check_image_root
    from process_activity import record_process
    with record_process("luxoptica-mail") as receipt:
        check_image_root(root)
        root.mkdir(parents=True, exist_ok=True)
        checkpoint()
        with acquire(FileLock(root / ".mail.lock", timeout=120)), incoming_lock(root):
            summary = _download_luxoptica_mail_attachments(sender_hint, subject_hint, lookback_days, top_messages)
            receipt["counts"] = {"Correos revisados": summary.messages_scanned,
                                 "Adjuntos descargados": summary.attachments_downloaded}
            return summary


def _download_luxoptica_mail_attachments(
    sender_hint: str, subject_hint: str, lookback_days: int, top_messages: int,
) -> DownloadSummary:
    config = load_m365_config()
    missing = validate_m365_config(config)
    if missing:
        raise RuntimeError(f"Faltan variables de entorno de Microsoft 365: {', '.join(missing)}")

    checkpoint()
    token = _get_access_token(config)
    checkpoint()

    root = config.download_root
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    root.mkdir(parents=True, exist_ok=True)
    from repository_storage import state_root
    state_file = state_root(root) / ".mail_download_state.json"
    state = _load_state(state_file)
    processed_ids: set[str] = set(state.get("processed_message_ids", []))
    pending_read: set[str] = set(state.get("pending_read_ids", []))

    def acknowledge(message_id: str) -> None:
        checkpoint()
        _mark_message_read(token, config.mailbox, message_id)
        pending_read.discard(message_id)
        state["pending_read_ids"] = sorted(pending_read)
        _save_state(state_file, state)

    def completed(message_id: str, *, mark_read: bool = True) -> None:
        processed_ids.add(message_id)
        newly_processed.append(message_id)
        if mark_read:
            pending_read.add(message_id)
        state["processed_message_ids"] = sorted(processed_ids)
        state["pending_read_ids"] = sorted(pending_read)
        _save_state(state_file, state)
        if mark_read:
            acknowledge(message_id)

    for message_id in sorted(pending_read):
        acknowledge(message_id)

    checkpoint()
    messages = _list_inbox_messages(token, config.mailbox, top_messages)
    pending_completed, pending_created = _refresh_pending_market_images(root)
    if pending_completed or pending_created:
        print(
            f"   Pendientes revisados: {pending_completed} completados, "
            f"{pending_created} copias de mercado creadas.",
            flush=True,
        )

    now_utc = datetime.now(timezone.utc)
    min_dt = now_utc - timedelta(days=max(1, lookback_days))
    sender_hint_lower = sender_hint.strip().lower()
    subject_hint_lower = subject_hint.strip().lower()

    attachments_downloaded = 0
    messages_with_attachments = 0
    saved_paths: list[str] = []
    newly_processed: list[str] = []

    for msg in messages:
        checkpoint()
        message_id = msg.get("id", "")
        if not message_id or message_id in processed_ids or msg.get("isRead", False):
            continue

        received_at = _parse_graph_dt(msg.get("receivedDateTime", ""))
        if received_at < min_dt:
            continue

        sender_addr = (
            msg.get("from", {})
            .get("emailAddress", {})
            .get("address", "")
            .strip()
            .lower()
        )
        subject = (msg.get("subject", "") or "").strip()

        if sender_hint_lower and sender_hint_lower not in sender_addr:
            if subject_hint_lower and subject_hint_lower not in subject.lower():
                continue

        if not msg.get("hasAttachments", False):
            body = _get_message_body(token, config.mailbox, message_id)
            target_dir = work_root(root) / received_at.astimezone(timezone.utc).strftime("%Y-%m-%d") / _detect_lote_from_subject(subject)
            check_work_root(work_root(root))
            target_dir.mkdir(parents=True, exist_ok=True)
            downloaded_from_link = False
            links = _find_download_links(body)
            for link in links:
                checkpoint()
                archive_path = _download_zip_link(link, target_dir)
                if archive_path is None:
                    continue
                saved_paths.append(str(archive_path))
                saved_paths.extend(str(path) for path in _extract_zip(archive_path, root))
                attachments_downloaded += 1
                downloaded_from_link = True
                break
            if downloaded_from_link:
                completed(message_id)
            else:
                print(
                    f"   ⚠️ Correo no leído sin ZIP de imágenes detectable: {subject or '[sin asunto]'} "
                    f"({len(links)} enlace(s))",
                    flush=True,
                )
            continue

        attachments = _list_attachments(token, config.mailbox, message_id)
        file_attachments = [
            att
            for att in attachments
            if att.get("@odata.type") == "#microsoft.graph.fileAttachment" and not att.get("isInline", False)
        ]

        if not file_attachments:
            completed(message_id, mark_read=False)
            continue

        messages_with_attachments += 1
        lote_folder = _detect_lote_from_subject(subject)

        for att in file_attachments:
            checkpoint()
            att_id = att.get("id", "")
            file_name = att.get("name", "attachment.bin")
            if not att_id:
                continue

            if att.get("contentBytes"):
                if len(att["contentBytes"]) > 4 * ((archive_limit() + 2) // 3):
                    raise ValueError("Adjunto supera GRAPH_IMAGE_MAX_ARCHIVE_BYTES")
                saved = _save_attachment_bytes(base64.b64decode(att["contentBytes"]), root,
                                               received_at, lote_folder, file_name)
            else:
                target_dir = work_root(root) / received_at.astimezone(timezone.utc).strftime("%Y-%m-%d") / lote_folder
                with requests.get(
                    _attachment_value_url(config.mailbox, message_id, att_id),
                    headers=_graph_headers(token),
                    timeout=request_timeout(60), stream=True,
                ) as value_resp:
                    value_resp.raise_for_status()
                    saved = _save_streamed_attachment(value_resp.iter_content(1024 * 1024), target_dir, file_name)
            saved_paths.append(str(saved))
            attachments_downloaded += 1

            if saved.suffix.lower() == ".zip":
                extracted_paths = _extract_zip(saved, root)
                saved_paths.extend(str(path) for path in extracted_paths)
            elif saved.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                if saved.stat().st_size > positive_setting("GRAPH_IMAGE_MAX_ENTRY_BYTES", 30 * 1024**2):
                    raise ValueError("Imagen supera GRAPH_IMAGE_MAX_ENTRY_BYTES; adjunto conservado")
                key = _image_model_color_key(saved.name)
                ean = _load_eans_by_image_key().get(key) if key else None
                if not ean:
                    raise ValueError(f"EAN no identificado; adjunto conservado en {saved}")
                record, _ = ImageRepository(root).save(
                    ean, _sanitize_filename(file_name), saved.read_bytes(), provider="Luxoptica",
                    origin=f"Graph:{message_id}:{att_id}",
                    view=_market_view_from_image_name(file_name) or "unknown",
                    date=received_at.isoformat(), metadata={"modelo": _model_from_image_name(file_name)},
                    allow_invalid=True,
                )
                saved_paths.append(str(record.path))

        completed(message_id)

    return DownloadSummary(
        messages_scanned=len(messages),
        messages_with_attachments=messages_with_attachments,
        attachments_downloaded=attachments_downloaded,
        saved_paths=saved_paths,
        processed_message_ids=newly_processed,
    )
