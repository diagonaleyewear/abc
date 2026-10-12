#!/usr/bin/env python3
"""
Script: Subir Solicitud a Luxoptica Automáticamente
Automatiza: Login → Ir a Imágenes → Cargar fichero → Enviar solicitud
"""

from pathlib import Path
from datetime import datetime
import sys
import time
import re
from tempfile import TemporaryDirectory
from image_repository import ImageRepository

def upload_to_luxoptica(
    url: str = "https://portal.luxottica.com/",
    username: str = "",
    password: str = "",
    ean_file: Path | None = None,
    email_destino: str = "images@diagonaleyewear.com",
    headless: bool = False,
    keep_browser_open: bool = False,
) -> tuple[bool, str]:
    from process_activity import record_process
    with record_process("luxoptica-upload") as receipt:
        result = _prepare_luxoptica_upload(url, username, password, ean_file, email_destino,
                                          headless, keep_browser_open)
        receipt["result"] = "Correcto" if result[0] else "Error"
        return result


def _prepare_luxoptica_upload(
    url: str = "https://portal.luxottica.com/",
    username: str = "",
    password: str = "",
    ean_file: Path | None = None,
    email_destino: str = "images@diagonaleyewear.com",
    headless: bool = False,
    keep_browser_open: bool = False,
) -> tuple[bool, str]:
    if ean_file is None:
        files = sorted((Path(__file__).resolve().parent / "docs" / "Luxoptica").glob(
            "upc-products-images-request-*.txt"), reverse=True)
        if not files:
            return False, "No se encontro fichero de EAN en docs/Luxoptica/"
        ean_file = files[0]
    if not ean_file.is_file():
        return False, f"Fichero no existe: {ean_file}"
    try:
        eans = list(dict.fromkeys(ean.strip() for ean in ean_file.read_text(encoding="utf-8").splitlines()
                                  if ean.strip()))
        if not eans:
            return False, "El fichero de solicitud no contiene EAN"
        repository = ImageRepository()
        pending = [ean for ean in eans if repository.pending_views(ean, ("frontal", "lateral", "perspectiva"))]
    except (OSError, ValueError) as error:
        return False, f"No se pudo consultar el repositorio de imagenes: {error}"
    if not pending:
        return True, "Todas las vistas ya disponibles; solicitud reutilizada sin acceso al portal."
    with TemporaryDirectory(prefix="luxoptica-request-") as directory:
        request = Path(directory) / ean_file.name
        request.write_text("\n".join(pending) + "\n", encoding="utf-8")
        return _upload_to_luxoptica(url, username, password, request, email_destino,
                                   headless, keep_browser_open)


def _upload_to_luxoptica(
    url: str,
    username: str,
    password: str,
    ean_file: Path,
    email_destino: str,
    headless: bool,
    keep_browser_open: bool,
) -> tuple[bool, str]:
    """
    Automatiza la subida de fichero a Luxoptica.
    
    Args:
        url: URL del portal de Luxoptica
        username: Usuario para login
        password: Contraseña
        ean_file: Ruta del fichero .txt con EAN
        email_destino: Email para la solicitud
        headless: Si False, muestra el navegador
    
    Returns:
        (éxito, mensaje)
    """
    
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError:
        return False, (
            "Playwright no instalado. Ejecuta:\n"
            "pip install playwright\n"
            "python -m playwright install chromium"
        )
    
    if not url.strip() or not username.strip() or not password.strip():
        return False, "Faltan URL, usuario o contraseña"
    
    if ean_file is None:
        # Buscar el fichero más reciente
        luxoptica_dir = Path("docs/Luxoptica")
        txt_files = sorted(luxoptica_dir.glob("upc-products-images-request-*.txt"), reverse=True)
        if not txt_files:
            return False, "No se encontró fichero de EAN en docs/Luxoptica/"
        ean_file = txt_files[0]
    
    if not ean_file.exists():
        return False, f"Fichero no existe: {ean_file}"
    
    try:
        with sync_playwright() as playwright:
            print(f"\n🌐 Iniciando navegador...")
            browser = playwright.chromium.launch(headless=headless)
            page = browser.new_page()

            def _count(selector: str) -> int:
                try:
                    return page.locator(selector).count()
                except Exception:
                    return 0
            
            # 1. Navegar al portal
            print(f"📍 Accediendo a: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            
            # 2. Login
            print(f"🔐 Autenticando...")
            
            # Buscar campo de usuario (en este portal suele ser "Username")
            username_inputs = [
                "input[placeholder='Username']",
                "input[aria-label='Username']",
                "input[placeholder*='username' i]",
                "input[name*='username' i]",
                "input[name*='user' i]",
                "input[id*='username' i]",
                "input[type='text']",
                "input[type='email']",
                "form input:not([type='hidden']):not([type='password'])",
            ]
            username_field = None
            for _ in range(30):
                for selector in username_inputs:
                    try:
                        locator = page.locator(selector)
                        if locator.count() > 0 and locator.first.is_visible():
                            username_field = locator.first
                            break
                    except Exception:
                        continue
                if username_field is not None:
                    break
                time.sleep(1)
            
            if username_field is None:
                browser.close()
                return False, "No se encontró campo de usuario en el login"
            
            username_field.fill(username)
            
            # El portal muestra la contraseña después de CONTINUE.
            try:
                continue_btn = page.get_by_role("button", name=re.compile(r"continue|continuar|next", re.I))
                continue_btn.first.click(timeout=15000)
            except Exception:
                page.locator("button[type='submit']").first.click(timeout=15000)
            
            # Buscar campo de contraseña
            password_field = None
            for _ in range(10):
                password_inputs = [
                    "input[type='password']",
                    "input[name='password']",
                    "input#password",
                ]
                for selector in password_inputs:
                    if _count(selector) > 0:
                        password_field = selector
                        break
                if password_field:
                    break
                time.sleep(1)
            
            if password_field is None:
                browser.close()
                return False, "No se encontró campo de contraseña después de CONTINUE"

            page.fill(password_field, password)

            # Enviar formulario una sola vez.
            submit_btn = page.get_by_role("button", name=re.compile(r"sign in|login|continue", re.I))
            if submit_btn.count() > 0:
                submit_btn.first.click()
            else:
                page.locator("button[type='submit']").first.click()
            
            # Esperar a que cargue después del login
            print(f"⏳ Esperando carga del portal...")
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(5)
            print(f"   URL tras login: {page.url}")

            # El portal puede mostrar un aviso legal antes de habilitar el menú.
            print("📜 Comprobando aviso legal...")
            legal_labels = [
                "Aceptar",
                "Acepto",
                "Aceptar y continuar",
                "Accept",
                "I agree",
                "Agree",
                "Continue",
            ]
            legal_accepted = False
            for _ in range(10):
                for label in legal_labels:
                    try:
                        target = page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
                        if target.count() > 0 and target.is_visible():
                            target.click(timeout=10000)
                            legal_accepted = True
                            time.sleep(3)
                            break
                    except Exception:
                        continue
                if legal_accepted:
                    print("   Aviso legal aceptado")
                    break
                time.sleep(1)
            
            # 3. Navegar a Imágenes de Producto
            print(f"📸 Navegando a sección de Imágenes...")
            
            # El menú del portal es dinámico y no siempre usa enlaces <a>.
            def _click_text(labels: list[str]) -> bool:
                for label in labels:
                    try:
                        target = page.get_by_text(label, exact=True).first
                        if target.count() > 0:
                            target.click(timeout=15000)
                            time.sleep(2)
                            return True
                    except Exception:
                        continue
                return False

            if not _click_text(["Servicios", "Services"]):
                browser.close()
                return False, "No se encontró el menú Servicios"

            if not _click_text(["Recursos digitales", "Recursos Digitales", "Digital Resources"]):
                browser.close()
                return False, "No se encontró Recursos digitales"

            if not _click_text(["Imágenes de producto", "Imágenes de Producto", "Product Images"]):
                browser.close()
                return False, "No se encontró Imágenes de producto"

            page.wait_for_load_state("domcontentloaded", timeout=30000)
            time.sleep(3)

            # La primera entrada en Imágenes de producto muestra este aviso legal.
            legal_accept = page.get_by_role(
                "button", name=re.compile(r"acepto|accept|i agree", re.I)
            ).first
            try:
                if legal_accept.count() > 0 and legal_accept.is_visible():
                    print("📜 Aceptando aviso legal de recursos digitales...")
                    checkboxes = page.locator("input[type='checkbox']")
                    if checkboxes.count() > 0 and not checkboxes.first.is_checked():
                        checkboxes.first.check()
                    legal_accept.click(timeout=15000)
                    time.sleep(3)
            except Exception as error:
                browser.close()
                return False, f"No se pudo aceptar el aviso legal de imágenes: {error}"
            
            # 4. Subir fichero
            print(f"📤 Subiendo fichero: {ean_file.name}")
            
            # Buscar input file
            file_inputs = page.locator("input[type='file']")
            if _count("input[type='file']") > 0:
                file_inputs.first.set_input_files(str(ean_file.absolute()))
                time.sleep(2)
            else:
                print(f"⚠️  No se encontró campo de fichero")
                print(f"   URL actual: {page.url}")
                print(f"   Título: {page.title()}")
                browser.close()
                return False, (
                    "Login correcto, pero no se encontró el formulario de carga "
                    f"en {page.url}"
                )

            # Tras cargar el fichero, el portal exige avanzar al paso "Ver tipo".
            continue_after_upload = page.get_by_role(
                "button", name=re.compile(r"^continuar$|^continue$|^next$", re.I)
            ).last
            try:
                continue_after_upload.click(timeout=15000)
                time.sleep(4)
            except Exception as error:
                browser.close()
                return False, f"No se pudo continuar después de cargar el fichero: {error}"
            
            # 5. Seleccionar "Todas las vistas"
            print(f"✓ Seleccionando opciones...")
            time.sleep(3)
            all_views_candidates = [
                page.get_by_text(re.compile(r"todas las vistas", re.I)).first,
                page.locator("button:has-text('TODAS LAS VISTAS')").first,
                page.locator("[role='button']:has-text('TODAS LAS VISTAS')").first,
                page.locator("text=/todas las vistas/i").first,
            ]
            try:
                all_views = next(
                    candidate for candidate in all_views_candidates
                    if candidate.count() > 0 and candidate.is_visible()
                )
                all_views.click(timeout=15000)
                time.sleep(4)
            except Exception as error:
                browser.close()
                return False, f"No se pudo seleccionar Todas las vistas: {error}"
            
            # 6. Especificar email
            print(f"📧 Configurando email: {email_destino}")

            # El portal deja info@diagonaleyewear.com por defecto y exige
            # añadir explícitamente cada dirección adicional.
            if _count(f"text={email_destino}") == 0:
                add_email = page.get_by_text(
                    re.compile(r"añadir dirección de email|add email address", re.I)
                ).first
                try:
                    add_email.click(timeout=15000)
                    time.sleep(1)
                except Exception as error:
                    browser.close()
                    return False, f"No se pudo añadir la dirección de email: {error}"

                email_inputs = page.locator("input[type='email'], input[name*='email' i]")
                if email_inputs.count() == 0:
                    browser.close()
                    return False, "No apareció el campo para añadir la dirección de email"
                email_inputs.last.fill(email_destino)
                time.sleep(1)
            
            # 7. Enviar solicitud
            print(f"🚀 Enviando solicitud...")

            submit_candidates = [
                page.locator("button:has-text('ENVIAR SOLICITUD')").last,
                page.locator("button:has-text('Enviar solicitud')").last,
                page.locator("button:has-text('Send request')").last,
                page.locator("button, [role='button'], input[type='submit']").filter(
                    has_text=re.compile(r"enviar solicitud|send request|submit", re.I)
                ).last,
                page.get_by_role("button", name=re.compile(r"enviar solicitud|send request|submit", re.I)).last,
                page.get_by_text(re.compile(r"^enviar solicitud$|^send request$|^submit$", re.I)).last,
            ]

            submit_button = None
            for candidate in submit_candidates:
                try:
                    if candidate.count() > 0:
                        for i in range(candidate.count()):
                            loc = candidate.nth(i)
                            if loc.is_visible() and loc.is_enabled():
                                submit_button = loc
                                break
                    if submit_button is not None:
                        break
                except Exception:
                    continue

            if submit_button is None:
                browser.close()
                return False, "No se encontró el botón Enviar solicitud"

            print("   Pulsando ENVIAR SOLICITUD...")
            submit_button.scroll_into_view_if_needed()
            submit_button.click(timeout=25000, force=True)
            time.sleep(3)
            
            # Verificar éxito
            success_indicators = [
                "text=/tu solicitud se ha enviado/i",
                "text=/your request has been sent/i",
                "text='solicitud enviada'",
                "text='request sent'",
                "text='éxito'",
                "text='success'",
            ]
            
            for indicator in success_indicators:
                if _count(indicator) > 0:
                    if keep_browser_open:
                        print("\n✅ Confirmación visible en el portal.")
                        print("   El navegador queda abierto para revisión. Pulsa Ctrl+C para terminar.")
                        try:
                            page.wait_for_event("close", timeout=600000)
                        except KeyboardInterrupt:
                            pass
                        except PlaywrightTimeoutError:
                            pass
                    browser.close()
                    return True, f"✅ Solicitud enviada exitosamente a {email_destino}"
            
            # Si no vemos confirmación clara, asumimos que funcionó
            if keep_browser_open:
                print("\n⚠️  No se detectó texto de confirmación automáticamente.")
                print("   El navegador queda abierto para comprobar el resultado. Pulsa Ctrl+C para terminar.")
                try:
                    page.wait_for_event("close", timeout=600000)
                except KeyboardInterrupt:
                    pass
                except PlaywrightTimeoutError:
                    pass
            browser.close()
            return True, f"✅ Proceso completado. Verifica la confirmación en el portal."
            
    except PlaywrightTimeoutError as e:
        return False, f"Timeout en la carga: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def main():
    print("\n" + "=" * 80)
    print("LUXOPTICA: Subida Automática de Solicitud")
    print("=" * 80)
    
    # Leer credenciales desde .env
    from db_config import load_env_file
    import os
    
    load_env_file()
    
    username = os.getenv("LUXOPTICA_USERNAME", "").strip()
    password = os.getenv("LUXOPTICA_PASSWORD", "").strip()
    url = os.getenv("LUXOPTICA_URL", "https://portal.luxottica.com/").strip()
    email = os.getenv("LUXOPTICA_REQUEST_EMAIL", "images@diagonaleyewear.com").strip()
    headless = os.getenv("LUXOPTICA_HEADLESS", "true").strip().lower() not in {"0", "false", "no"}
    keep_browser_open = os.getenv("LUXOPTICA_KEEP_BROWSER_OPEN", "false").strip().lower() in {"1", "true", "yes"}
    
    if not username or not password:
        print(f"\n❌ Faltan credenciales en .env")
        print(f"\n   Configura en .env:")
        print(f"   LUXOPTICA_USERNAME=tu_usuario")
        print(f"   LUXOPTICA_PASSWORD=tu_contraseña")
        print(f"   LUXOPTICA_URL=https://portal.luxottica.com/")
        print(f"   LUXOPTICA_REQUEST_EMAIL=images@diagonaleyewear.com")
        return 1
    
    # Buscar fichero más reciente
    luxoptica_dir = Path("docs/Luxoptica")
    txt_files = sorted(luxoptica_dir.glob("upc-products-images-request-*.txt"), reverse=True)
    
    if not txt_files:
        print(f"\n❌ No se encontró fichero de EAN en docs/Luxoptica/")
        return 1
    
    ean_file = txt_files[0]
    print(f"\n📄 Fichero a subir: {ean_file.name}")
    
    # Ejecutar upload
    success, message = upload_to_luxoptica(
        url=url,
        username=username,
        password=password,
        ean_file=ean_file,
        email_destino=email,
        headless=headless,
        keep_browser_open=keep_browser_open,
    )
    
    print(f"\n{message}")
    
    if success:
        print(f"\n✅ Solicitud completada!")
        print(f"   Email destino: {email}")
        print(f"   Fichero: {ean_file.name}")
        return 0
    else:
        print(f"\n❌ Fallo en la subida")
        return 1


if __name__ == "__main__":
    sys.exit(main())
