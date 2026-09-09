"""Importa terceiros da Gestão de Terceiros NIO (SymSupply/Nashai) para Gestão de Usuários.

Fluxo: reutiliza cookies salvos; se expirarem, faz login automático no IdP V.tal
com matrícula/senha PAP de um usuário Diretoria (sem FAST PASS/OTP se possível).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q
from django.utils.crypto import get_random_string

from usuarios.models import Perfil, Usuario

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

NIO_LOGIN_HINTS = (
    "login.vtal.com",
    "efetuar login",
    "senha + otp",
    "v.tal fast pass",
)

_login_lock = threading.Lock()

FUNCAO_PARA_PERFIL = (
    ("DIRETOR", "Diretoria"),
    ("SUPERVISOR DE VENDAS", "Supervisor"),
    ("SUPERVISOR", "Supervisor"),
    ("OPERADOR BACK-OFFICE", "BackOffice"),
    ("OPERADOR BACKOFFICE", "BackOffice"),
    ("OPERADOR VENDA INTERNA", "Vendedor"),
    ("VENDEDOR", "Vendedor"),
)

FUNCAO_PARA_CANAL = {
    "VENDEDOR": "PAP",
    "OPERADOR VENDA INTERNA": "PAP",
}


class SessaoNioExpirada(RuntimeError):
    """Cookies da Gestão de Terceiros expiraram ou login automático falhou."""


class NioTerceirosError(RuntimeError):
    pass


def _cfg(nome: str, default: Any = None) -> Any:
    return getattr(settings, nome, default)


def storage_state_path() -> Path:
    configurado = _cfg("NIO_TERCEIROS_STORAGE_STATE")
    if configurado:
        return Path(configurado)
    base = Path(_cfg("PAP_SESSIONS_DIR") or "pap_sessions")
    return base / "nio_gestaodeterceiros_session.json"


def cache_path() -> Path:
    configurado = _cfg("NIO_TERCEIROS_CACHE")
    if configurado:
        return Path(configurado)
    return storage_state_path().with_name("nio_terceiros_cache.json")


def empresa_id() -> str:
    return str(_cfg("NIO_TERCEIROS_EMPRESA_ID", "370721")).strip() or "370721"


def base_url() -> str:
    return str(_cfg("NIO_TERCEIROS_BASE_URL", "https://gestaodeterceiros.nashai.ai")).rstrip("/")


def obter_usuario_diretor() -> Usuario | None:
    """Usuário Diretoria com matrícula/senha PAP para login na Gestão de Terceiros."""
    matricula_cfg = str(_cfg("NIO_TERCEIROS_MATRICULA_DIRETOR") or "").strip().upper()
    base = (
        Usuario.objects.filter(is_active=True)
        .filter(Q(perfil__nome__iexact="Diretoria") | Q(groups__name__iexact="Diretoria"))
        .exclude(Q(matricula_pap__isnull=True) | Q(matricula_pap__exact=""))
        .exclude(Q(senha_pap__isnull=True) | Q(senha_pap__exact=""))
        .distinct()
    )
    if matricula_cfg:
        encontrado = base.filter(matricula_pap__iexact=matricula_cfg).first()
        if encontrado:
            return encontrado
    return base.order_by("id").first()


def _cookies_do_storage_state(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SessaoNioExpirada(
            "Sessão da Gestão de Terceiros NIO não encontrada. "
            "Será necessário login automático com o Diretor."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessaoNioExpirada(f"Arquivo de sessão NIO inválido: {exc}") from exc
    cookies: dict[str, str] = {}
    for item in data.get("cookies") or []:
        dominio = (item.get("domain") or "").lower()
        nome = item.get("name") or ""
        valor = item.get("value")
        if not nome or valor is None:
            continue
        if "nashai" in dominio or "gestaodeterceiros" in dominio or dominio in ("", ".nashai.ai"):
            cookies[nome] = str(valor)
    if not cookies:
        raise SessaoNioExpirada("A sessão salva não tem cookies da Gestão de Terceiros NIO.")
    return cookies


def _garantir_html_autenticado(html: str, url: str) -> str:
    if pagina_login_vtal(html=html, url=url):
        raise SessaoNioExpirada(
            "Sessão da Gestão de Terceiros NIO expirou (redirecionado ao IdP V.tal)."
        )
    return html


def _session_http() -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) site-clickup-nio-terceiros",
            "Accept": "text/html,application/xhtml+xml",
        }
    )
    for nome, valor in _cookies_do_storage_state(storage_state_path()).items():
        sessao.cookies.set(nome, valor, domain="gestaodeterceiros.nashai.ai")
    return sessao


def fetch_html(url: str, sessao: requests.Session | None = None) -> str:
    cliente = sessao or _session_http()
    resposta = cliente.get(url, timeout=45, allow_redirects=True)
    return _garantir_html_autenticado(resposta.text or "", resposta.url or url)


def url_lista(pagina: int = 0) -> str:
    return (
        f"{base_url()}/empresas.php?pagina=colaboradores&id={empresa_id()}"
        f"&p={int(pagina)}&busca_status=-1&busca_avaliacao=-1"
    )


def url_cadastro(nio_id: str) -> str:
    return (
        f"{base_url()}/empresas.php?pagina=colaboradores&id={empresa_id()}"
        f"&colaborador={nio_id}&ac=editar"
    )


def url_home_empresa() -> str:
    return (
        f"{base_url()}/empresas.php?pagina=empresas&id={empresa_id()}"
        f"&ac=emp_home&land_corporativo=15000&land_empresa={empresa_id()}"
    )


def probe_sessao_valida() -> bool:
    """True se os cookies atuais abrem a lista de colaboradores sem cair no IdP."""
    try:
        html = fetch_html(url_lista(0))
    except SessaoNioExpirada:
        return False
    except Exception as exc:
        logger.warning("[NIO terceiros] Probe de sessão falhou: %s", exc)
        return False
    return bool(re.search(r"colaborador=\d+", html, re.I) or "Colaboradores" in html)


def _pagina_vtal_travada(html: str, url: str) -> bool:
    alvo = f"{url} {html}".lower()
    if "login.vtal.com" not in alvo and "nidp" not in alvo:
        return False
    sinais = (
        "processando o login",
        "fast pass",
        "memorize o código",
        "memorize o codigo",
        "v.tal fast pass",
    )
    return any(s in alvo for s in sinais)


def _preencher_login_vtal(page, matricula: str, senha: str) -> None:
    for sel in (
        "#inputMatricula",
        'input[placeholder*="Login"]',
        'input[name*="matricula"]',
        'input[name*="username"]',
        'input[type="text"]:not([type="search"])',
    ):
        try:
            page.fill(sel, matricula, timeout=5000)
            break
        except Exception:
            continue
    for sel in ("#passwordInput", 'input[type="password"]', 'input[placeholder*="Senha"]'):
        try:
            page.fill(sel, senha, timeout=5000)
            break
        except Exception:
            continue
    clicou = False
    for sel_btn in (
        'button:has-text("EFETUAR")',
        'button:has-text("Efetuar login")',
        'button:has-text("Entrar")',
        'button[type="submit"]',
        'input[type="submit"]',
    ):
        try:
            page.click(sel_btn, timeout=5000)
            clicou = True
            break
        except Exception:
            continue
    if not clicou:
        page.keyboard.press("Enter")


def _selecionar_ambiente_nio(page) -> None:
    url = (page.url or "").lower()
    if "escolher_corporativo" in url or "gestaodeterceiros" in url:
        for seletor in (
            'a:has-text("NIO")',
            'a[href*="land_corporativo"]',
            'a[href*="empresas.php"]',
        ):
            try:
                loc = page.locator(seletor).first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.click(timeout=8000)
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue


def renovar_sessao_login_diretor() -> Path:
    """Playwright: login V.tal com credenciais do Diretor e grava storage_state."""
    if not HAS_PLAYWRIGHT:
        raise NioTerceirosError("Playwright não está instalado no servidor.")

    diretor = obter_usuario_diretor()
    if not diretor:
        raise NioTerceirosError(
            "Nenhum usuário Diretoria com matrícula e senha PAP cadastrados "
            "para login automático na Gestão de Terceiros."
        )
    matricula = (diretor.matricula_pap or "").strip()
    senha = (diretor.senha_pap or "").strip()
    if not matricula or not senha:
        raise NioTerceirosError("Credenciais PAP do Diretor incompletas.")

    path = storage_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    headless = bool(_cfg("NIO_TERCEIROS_HEADLESS", True))
    logger.info(
        "[NIO terceiros] Renovando sessão com Diretor %s (matrícula %s, headless=%s)",
        diretor.username,
        matricula,
        headless,
    )

    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        if headless:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
        page = context.new_page()
        page.set_default_timeout(25000)
        page.goto(url_lista(0), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)

        url_atual = page.url or ""
        html = ""
        try:
            html = page.content() or ""
        except Exception:
            pass

        if pagina_login_vtal(html=html, url=url_atual) or "login.vtal.com" in url_atual.lower():
            _preencher_login_vtal(page, matricula, senha)
            page.wait_for_timeout(3500)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            url_atual = page.url or ""
            try:
                html = page.content() or ""
            except Exception:
                html = ""
            if _pagina_vtal_travada(html, url_atual):
                raise SessaoNioExpirada(
                    "Login V.tal travado em FAST PASS/OTP. "
                    "Use um Diretor com login senha sem FAST PASS ou aprove no app e tente de novo."
                )
            if pagina_login_vtal(html=html, url=url_atual):
                raise SessaoNioExpirada(
                    "Falha no login automático do Diretor na V.tal. "
                    "Verifique matrícula/senha PAP do perfil Diretoria."
                )

        _selecionar_ambiente_nio(page)
        page.goto(url_lista(0), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        url_final = page.url or ""
        html_final = ""
        try:
            html_final = page.content() or ""
        except Exception:
            pass
        if pagina_login_vtal(html=html_final, url=url_final):
            raise SessaoNioExpirada(
                "Após o login, a sessão ainda está no IdP V.tal. Tente novamente em instantes."
            )
        if not (re.search(r"colaborador=\d+", html_final, re.I) or "Colaboradores" in html_final):
            # Pode ainda estar em escolher_corporativo
            _selecionar_ambiente_nio(page)
            page.goto(url_lista(0), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            html_final = page.content() or ""
            url_final = page.url or ""
            if pagina_login_vtal(html=html_final, url=url_final):
                raise SessaoNioExpirada("Sessão NIO não autenticou após selecionar o ambiente.")

        context.storage_state(path=str(path))
        logger.info("[NIO terceiros] Sessão salva em %s", path)
        return path
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass


def garantir_sessao_nio(forcar_relogin: bool = False) -> None:
    """Garante cookies válidos; se necessário, login automático com o Diretor."""
    with _login_lock:
        if not forcar_relogin and probe_sessao_valida():
            return
        renovar_sessao_login_diretor()
        if not probe_sessao_valida():
            raise SessaoNioExpirada(
                "Login do Diretor concluiu, mas a lista de terceiros ainda não autenticou."
            )


def digits_only(valor: str | None) -> str:
    return re.sub(r"\D+", "", valor or "")


def normalizar_cpf(valor: str | None) -> str:
    cpf = digits_only(valor)
    return cpf if len(cpf) == 11 else cpf


def normalizar_celular(valor: str | None) -> str:
    numero = digits_only(valor)
    if len(numero) == 10 and numero[2:3] != "9":
        numero = numero[:2] + "9" + numero[2:]
    if numero.startswith("55") and len(numero) >= 12:
        numero = numero[2:]
    return numero


def sem_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def split_nome(nome: str) -> tuple[str, str]:
    partes = [p for p in (nome or "").strip().split() if p]
    if not partes:
        return "", ""
    if len(partes) == 1:
        return partes[0].title(), ""
    return partes[0].title(), " ".join(partes[1:]).title()


def mapear_perfil(funcao: str) -> str:
    chave = sem_acentos((funcao or "").upper()).strip()
    for trecho, perfil in FUNCAO_PARA_PERFIL:
        if trecho in chave:
            return perfil
    return "Vendedor"


def mapear_canal(funcao: str) -> str:
    chave = sem_acentos((funcao or "").upper()).strip()
    for trecho, canal in FUNCAO_PARA_CANAL.items():
        if trecho in chave:
            return canal
    return "PARCEIRO"


def pagina_login_vtal(html: str = "", url: str = "") -> bool:
    alvo = f"{url} {html}".lower()
    return any(hint in alvo for hint in NIO_LOGIN_HINTS)


def parse_lista_html(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    encontrados: dict[str, dict[str, str]] = {}
    for link in soup.select('a[href*="colaborador="]'):
        href = link.get("href") or ""
        if "ac=editar" not in href and "ac=editar" not in href.replace("&amp;", "&"):
            continue
        match = re.search(r"colaborador=(\d+)", href.replace("&amp;", "&"))
        if not match:
            continue
        nio_id = match.group(1)
        nome = " ".join((link.get_text() or "").split())
        if not nome or nome.lower() == "cadastro":
            continue
        bloco = link.find_parent(class_=re.compile(r"col-md-11")) or link.parent
        texto = " ".join((bloco.get_text(" ", strip=True) if bloco else nome).split())
        chave = ""
        chave_match = re.search(r"Chave de Acesso:\s*([A-Z]{2}\d+)", texto, re.I)
        if chave_match:
            chave = chave_match.group(1).upper()
        funcao = ""
        if bloco:
            for div in bloco.find_all("div", class_=re.compile(r"col-md-2")):
                candidato = " ".join(div.get_text(" ", strip=True).split())
                if re.search(
                    r"VENDEDOR|SUPERVISOR|DIRETOR|OPERADOR|BACK-OFFICE|BACKOFFICE",
                    candidato,
                    re.I,
                ):
                    funcao = candidato.replace("\xa0", " ").strip()
                    break
        status = "Ativado" if re.search(r"\bAtivado\b", texto, re.I) else ""
        encontrados[nio_id] = {
            "nio_id": nio_id,
            "nome": nome.upper(),
            "matricula": chave,
            "funcao": funcao,
            "status": status,
            "local": "MG" if re.search(r"\bMG\b", texto) else "",
        }
    return list(encontrados.values())


def _valor_campo(soup: BeautifulSoup, name: str) -> str:
    el = soup.find(attrs={"name": name})
    if not el:
        return ""
    if el.name == "select":
        selecionada = el.find("option", selected=True) or el.find("option", selected="selected")
        if selecionada:
            return " ".join(selecionada.get_text(" ", strip=True).split())
        atual = el.get("value")
        if atual:
            opt = el.find("option", value=atual)
            if opt:
                return " ".join(opt.get_text(" ", strip=True).split())
        return ""
    return (el.get("value") or "").strip()


def parse_cadastro_html(html: str, nio_id: str = "") -> dict[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")
    chave = ""
    for inp in soup.select('input[type="text"]'):
        valor = (inp.get("value") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}\d+", valor):
            chave = valor
            break
    funcao = _valor_campo(soup, "funcao_ctps")
    return {
        "nio_id": nio_id,
        "matricula": chave,
        "nome": (_valor_campo(soup, "nome") or "").upper(),
        "cpf": normalizar_cpf(_valor_campo(soup, "cpf")),
        "email": (_valor_campo(soup, "email") or "").strip().lower(),
        "celular": normalizar_celular(_valor_campo(soup, "mobile_phone")),
        "funcao": funcao,
        "vinculo": _valor_campo(soup, "tipo_vinculo"),
        "perfil_nio": _valor_campo(soup, "perfil"),
        "data_nascimento": _valor_campo(soup, "data_nascimento"),
        "status": _valor_campo(soup, "status") or "Ativado",
    }


def mesclar_terceiro(base: dict[str, str], detalhe: dict[str, str]) -> dict[str, str]:
    out = dict(base)
    for chave, valor in (detalhe or {}).items():
        if valor:
            out[chave] = valor
    out["perfil_sugerido"] = mapear_perfil(out.get("funcao") or "")
    out["canal_sugerido"] = mapear_canal(out.get("funcao") or "")
    return out


def _cpf_usuario(usuario: Usuario) -> str:
    return normalizar_cpf(getattr(usuario, "cpf", None))


def localizar_usuario(terceiro: dict[str, str], usuarios: list[Usuario]) -> Usuario | None:
    cpf = normalizar_cpf(terceiro.get("cpf"))
    matricula = (terceiro.get("matricula") or "").strip().upper()
    if cpf:
        for usuario in usuarios:
            if _cpf_usuario(usuario) == cpf:
                return usuario
    if matricula:
        for usuario in usuarios:
            atual = (getattr(usuario, "matricula_pap", None) or "").strip().upper()
            if atual == matricula:
                return usuario
    return None


def username_candidato(nome: str, matricula: str, usados: set[str]) -> str:
    first, last = split_nome(nome)
    base = re.sub(r"[^\w.@+-]", "", sem_acentos(first)) or "user"
    candidatos = [base]
    if last:
        candidatos.append(f"{base}.{re.sub(r'[^\w]', '', sem_acentos(last.split()[0]))}")
    if matricula:
        candidatos.append(matricula)
    for candidato in candidatos:
        chave = candidato.lower()
        if chave and chave not in usados:
            usados.add(chave)
            return candidato
    extra = f"{base}{get_random_string(4)}"
    usados.add(extra.lower())
    return extra


def email_cadastro(terceiro: dict[str, str]) -> str:
    email = (terceiro.get("email") or "").strip().lower()
    if email and "@" in email:
        return email
    matricula = (terceiro.get("matricula") or "sem-matricula").lower()
    return f"{matricula}@pendente.local"


def carregar_cache() -> dict[str, Any]:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def salvar_cache(terceiros: list[dict[str, str]], origem: str = "nio") -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "origem": origem,
        "empresa_id": empresa_id(),
        "terceiros": terceiros,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sincronizar_da_nio(
    incluir_cadastro: bool = True,
    pausa_s: float = 0.2,
    renovar_sessao: bool = True,
) -> list[dict[str, str]]:
    if renovar_sessao:
        garantir_sessao_nio(forcar_relogin=False)
    try:
        sessao = _session_http()
        html_lista = fetch_html(url_lista(0), sessao=sessao)
    except SessaoNioExpirada:
        if not renovar_sessao:
            raise
        garantir_sessao_nio(forcar_relogin=True)
        sessao = _session_http()
        html_lista = fetch_html(url_lista(0), sessao=sessao)
    terceiros = parse_lista_html(html_lista)
    if not terceiros:
        raise NioTerceirosError("Nenhum terceiro encontrado na lista da NIO.")
    if incluir_cadastro:
        for item in terceiros:
            try:
                html = fetch_html(url_cadastro(item["nio_id"]), sessao=sessao)
                detalhe = parse_cadastro_html(html, nio_id=item["nio_id"])
                item.update({k: v for k, v in mesclar_terceiro(item, detalhe).items() if v})
            except SessaoNioExpirada:
                raise
            except Exception:
                logger.exception("[NIO terceiros] Falha ao ler cadastro %s", item.get("nio_id"))
            if pausa_s:
                time.sleep(pausa_s)
    for item in terceiros:
        item["perfil_sugerido"] = mapear_perfil(item.get("funcao") or "")
        item["canal_sugerido"] = mapear_canal(item.get("funcao") or "")
    salvar_cache(terceiros)
    return terceiros


def terceiros_para_preview(terceiros: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    if terceiros is None:
        cache = carregar_cache()
        terceiros = list(cache.get("terceiros") or [])
    usuarios = list(Usuario.objects.all())
    saida: list[dict[str, Any]] = []
    for item in terceiros:
        normalizado = mesclar_terceiro(item, {})
        usuario = localizar_usuario(normalizado, usuarios)
        acao = "criar"
        observacao = ""
        if usuario:
            acao = "atualizar"
            faltas = []
            if not (usuario.matricula_pap or "").strip() and normalizado.get("matricula"):
                faltas.append("matrícula PAP")
            if not (usuario.cpf or "").strip() and normalizado.get("cpf"):
                faltas.append("CPF")
            if not (usuario.tel_whatsapp or "").strip() and normalizado.get("celular"):
                faltas.append("WhatsApp")
            if usuario.perfil_id is None and normalizado.get("perfil_sugerido"):
                faltas.append("perfil")
            if not faltas:
                acao = "ja_cadastrado"
                observacao = "Já existe na Gestão de Usuários."
            else:
                observacao = "Atualizar: " + ", ".join(faltas)
        else:
            observacao = "Novo cadastro a partir da NIO."
        saida.append(
            {
                **normalizado,
                "acao": acao,
                "observacao": observacao,
                "usuario_id": usuario.id if usuario else None,
                "usuario_username": usuario.username if usuario else "",
            }
        )
    return saida


def _aplicar_atualizacao(usuario: Usuario, terceiro: dict[str, str]) -> list[str]:
    alterados: list[str] = []
    matricula = (terceiro.get("matricula") or "").strip().upper()
    if matricula and not (usuario.matricula_pap or "").strip():
        usuario.matricula_pap = matricula
        alterados.append("matricula_pap")
    cpf = normalizar_cpf(terceiro.get("cpf"))
    if cpf and not digits_only(usuario.cpf):
        usuario.cpf = cpf
        alterados.append("cpf")
    celular = normalizar_celular(terceiro.get("celular"))
    if celular and not digits_only(usuario.tel_whatsapp):
        usuario.tel_whatsapp = celular
        alterados.append("tel_whatsapp")
    if usuario.perfil_id is None:
        perfil_nome = mapear_perfil(terceiro.get("funcao") or "")
        perfil = Perfil.objects.filter(nome__iexact=perfil_nome).first()
        if perfil:
            usuario.perfil = perfil
            grupo = Group.objects.filter(name__iexact=perfil.nome).first()
            usuario.save()
            if grupo:
                usuario.groups.set([grupo])
            alterados.append("perfil")
            return alterados
    if alterados:
        usuario.save(update_fields=alterados)
    return alterados


@transaction.atomic
def importar_terceiros(nio_ids: list[str], terceiros: list[dict[str, str]] | None = None) -> dict[str, Any]:
    if terceiros is None:
        cache = carregar_cache()
        terceiros = list(cache.get("terceiros") or [])
    por_id = {str(item.get("nio_id")): item for item in terceiros}
    ids = [str(i) for i in nio_ids if str(i) in por_id]
    if not ids:
        raise NioTerceirosError("Nenhum terceiro selecionado (sincronize a lista da NIO antes).")

    usuarios = list(Usuario.objects.all())
    usados = { (u.username or "").lower() for u in usuarios }
    emails = { (u.email or "").lower() for u in usuarios if u.email }

    criados: list[dict[str, Any]] = []
    atualizados: list[dict[str, Any]] = []
    ignorados: list[dict[str, Any]] = []
    erros: list[dict[str, Any]] = []

    for nio_id in ids:
        item = mesclar_terceiro(por_id[nio_id], {})
        try:
            usuario = localizar_usuario(item, usuarios)
            if usuario:
                campos = _aplicar_atualizacao(usuario, item)
                if campos:
                    atualizados.append(
                        {
                            "nio_id": nio_id,
                            "usuario_id": usuario.id,
                            "username": usuario.username,
                            "campos": campos,
                        }
                    )
                else:
                    ignorados.append(
                        {
                            "nio_id": nio_id,
                            "usuario_id": usuario.id,
                            "username": usuario.username,
                            "motivo": "já cadastrado",
                        }
                    )
                continue

            perfil_nome = mapear_perfil(item.get("funcao") or "")
            perfil = Perfil.objects.filter(nome__iexact=perfil_nome).first()
            if not perfil:
                raise NioTerceirosError(f"Perfil '{perfil_nome}' não existe na Gestão de Usuários.")
            first, last = split_nome(item.get("nome") or "")
            username = username_candidato(item.get("nome") or "", item.get("matricula") or "", usados)
            email = email_cadastro(item)
            if email.lower() in emails:
                email = f"{(item.get('matricula') or nio_id).lower()}@pendente.local"
            emails.add(email.lower())
            senha = get_random_string(10)
            novo = Usuario(
                username=username,
                first_name=first,
                last_name=last,
                email=email,
                cpf=normalizar_cpf(item.get("cpf")) or None,
                matricula_pap=(item.get("matricula") or "").strip().upper() or None,
                tel_whatsapp=normalizar_celular(item.get("celular")) or None,
                perfil=perfil,
                canal=mapear_canal(item.get("funcao") or ""),
                is_active=True,
                obriga_troca_senha=True,
            )
            novo.set_password(senha)
            novo.save()
            grupo = Group.objects.filter(name__iexact=perfil.nome).first()
            if grupo:
                novo.groups.set([grupo])
            usuarios.append(novo)
            criados.append(
                {
                    "nio_id": nio_id,
                    "usuario_id": novo.id,
                    "username": novo.username,
                    "nome": f"{first} {last}".strip(),
                    "matricula_pap": novo.matricula_pap,
                    "perfil": perfil.nome,
                    "senha_temporaria": senha,
                }
            )
        except Exception as exc:
            logger.exception("[NIO terceiros] Falha ao importar %s", nio_id)
            erros.append({"nio_id": nio_id, "erro": str(exc)})

    return {
        "criados": criados,
        "atualizados": atualizados,
        "ignorados": ignorados,
        "erros": erros,
    }
