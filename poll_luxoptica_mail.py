#!/usr/bin/env python3
"""Monitor periódico de correos de Luxottica.

Comprueba cada X minutos si ha llegado un correo con adjuntos y, en caso
afirmativo, los registra en IMAGE_REPOSITORY_ROOT por EAN.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from threading import Event
from datetime import datetime

from graph_mail_downloader import download_luxoptica_mail_attachments
from process_activity import record_process
from service_stop import StopRequested, checkpoint, stopping_with


LOGGER = logging.getLogger(__name__)


def run_once(sender_hint: str, subject_hint: str, lookback_days: int, top_messages: int) -> dict:
    summary = download_luxoptica_mail_attachments(
        sender_hint=sender_hint,
        subject_hint=subject_hint,
        lookback_days=lookback_days,
        top_messages=top_messages,
    )

    return {
        "messages_scanned": summary.messages_scanned,
        "messages_with_attachments": summary.messages_with_attachments,
        "attachments_downloaded": summary.attachments_downloaded,
        "saved_paths": summary.saved_paths,
        "processed_message_ids": summary.processed_message_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor de correo Luxottica")
    parser.add_argument("--interval-minutes", type=int, default=10, help="Cada cuántos minutos comprobar el buzón")
    parser.add_argument("--sender-hint", default="luxottica", help="Texto que debe contener el remitente")
    parser.add_argument("--subject-hint", default="image", help="Texto que debe contener el asunto")
    parser.add_argument("--lookback-days", type=int, default=7, help="Días de ventana a revisar")
    parser.add_argument("--top-messages", type=int, default=100, help="Máximo número de mensajes por comprobación")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola comprobación y sale")
    args = parser.parse_args()

    interval_seconds = max(30, args.interval_minutes * 60)
    stopping = Event()
    requested_at = None

    def request_stop(signum, frame):
        nonlocal requested_at
        if requested_at is None:
            requested_at = time.monotonic()
        stopping.set()

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with stopping_with(stopping):
            result = monitor(args, interval_seconds, stopping)
        if requested_at is not None and time.monotonic() - requested_at > 30:
            LOGGER.error("Parada excedio30s; captura bloqueada aunque el trabajo haya terminado")
            return 1
        return result
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def monitor(args: argparse.Namespace, interval_seconds: int, stopping: Event) -> int:
    print("=" * 80)
    print("MONITOR LUXOPTICA: comprobación periódica de correo")
    print("=" * 80)
    print(f"Intervalo: cada {args.interval_minutes} minuto(s)")
    print(f"Remitente: {args.sender_hint}")
    print(f"Asunto: {args.subject_hint}")
    print(f"Ventana: {args.lookback_days} día(s)")
    print(f"Top mensajes: {args.top_messages}")
    print("=" * 80)

    while not stopping.is_set():
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{timestamp}] Revisando buzón...")
        try:
            with record_process("luxoptica-monitor", enabled=not args.once, interval=interval_seconds) as receipt:
                checkpoint()
                summary = run_once(
                    sender_hint=args.sender_hint,
                    subject_hint=args.subject_hint,
                    lookback_days=args.lookback_days,
                    top_messages=args.top_messages,
                )
                receipt["counts"] = {"Correos revisados": summary["messages_scanned"],
                                     "Adjuntos descargados": summary["attachments_downloaded"]}
            print(f"  Correos revisados: {summary['messages_scanned']}")
            print(f"  Correos con adjuntos: {summary['messages_with_attachments']}")
            print(f"  Adjuntos descargados: {summary['attachments_downloaded']}")

            if summary["attachments_downloaded"] > 0:
                print("  ✅ Se han recibido adjuntos nuevos.")
                for path in summary["saved_paths"][:5]:
                    print(f"    - {path}")
                if len(summary["saved_paths"]) > 5:
                    print(f"    ... y {len(summary['saved_paths']) - 5} más")
            else:
                print("  ⏳ Todavía no hay adjuntos nuevos en el buzón.")
        except StopRequested:
            LOGGER.info("Parada drenada: trabajo pendiente conservado; no se inicia otro ciclo")
            break
        except Exception as exc:
            print(f"  ❌ Error al comprobar el buzón: {exc}")
            if stopping.is_set():
                LOGGER.error("Parada con fallo de operacion; revisar estado antes de captura", exc_info=True)
                return 1

        if args.once or stopping.is_set():
            break

        print(f"\nPróxima comprobación en {args.interval_minutes} minuto(s)...")
        stopping.wait(interval_seconds)

    LOGGER.info("Monitor detenido sin trabajos activos")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
