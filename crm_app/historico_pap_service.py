# crm_app/historico_pap_service.py
"""Busca o histórico PAP (venda / interesse / pré-venda) com a sessão do usuário.

Ritmo igual à tela: 15 por página, pausa entre páginas. Não abre Detalhar.
Não grava Venda — só protocolos em HistoricoPapPedido.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Optional, Tuple

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from crm_app.historico_pap import (
    LIMIT_PAGINA,
    MAX_DIAS_BUSCA,
    PAP_HISTORICO_URL,
    STATUS_LISTA_PADRAO,
    TIPO_API_ALIASES,
    extrair_lista_api,
    map_pedido_api,
    montar_url_vendas,
    montar_xlsx_historico,
    normalizar_pedido,
    parse_arquivo_exportacao,
    tipos_solicitados,
)

logger = logging.getLogger(__name__)

# Fallback em memória caso a tabela de cache (django_cache_table) não esteja inicializada
_IN_MEMORY_CACHE: dict[str, tuple[Any, float]] = {}


def _cache_get(key: str) -> Any:
    try:
        val = cache.get(key)
        if val is not None:
            return val
    except Exception:
        pass
    item = _IN_MEMORY_CACHE.get(key)
    if item:
        val, exp_ts = item
        if exp_ts > time.time():
            return val
        _IN_MEMORY_CACHE.pop(key, None)
    return None


def _cache_set(key: str, val: Any, timeout_seconds: int = 3600) -> None:
    exp_ts = time.time() + timeout_seconds
    _IN_MEMORY_CACHE[key] = (val, exp_ts)
    try:
        cache.set(key, val, timeout_seconds)
    except Exception:
        pass


def _cache_delete(key: str) -> None:
    _IN_MEMORY_CACHE.pop(key, None)
    try:
        cache.delete(key)
    except Exception:
        pass


def limpar_jwt(token: str) -> str:
    """Extrai a sequência pura de Base64/Base64URL do JWT, eliminando Bearer, aspas ou espaços."""
    if not token or not isinstance(token, str):
        return ""
    t = token.strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    t = t.strip("\"'`")
    m = re.search(r"eyJ[A-Za-z0-9_\-\+\/=]{5,}\.[A-Za-z0-9_\-\+\/=]{5,}\.[A-Za-z0-9_\-\+\/=]{5,}", t)
    if m:
        return m.group(0)
    if t.startswith("eyJ") and t.count(".") == 2:
        return t
    return ""


def validar_e_decodificar_jwt(token: str) -> tuple[bool, Optional[dict], str]:
    """
    Valida formato de JWT e verifica se está expirado.
    Retorna (valido, payload_dict, motivo_ou_token_limpo).
    """
    if not token or not isinstance(token, str):
        return False, None, "Token vazio ou formato inválido."
    clean = limpar_jwt(token)
    if not clean:
        return False, None, "Token não possui estrutura de JWT válido (esperado eyJ...)."
    parts = clean.split(".")
    payload_b64 = parts[1]
    rem = len(payload_b64) % 4
    if rem > 0:
        payload_b64 += "=" * (4 - rem)
    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:
        return False, None, f"Payload do JWT ilegível: {exc}"

    exp = payload.get("exp")
    if exp is not None:
        try:
            exp_ts = float(exp)
            agora = time.time()
            if exp_ts <= agora:
                dt_exp = datetime.fromtimestamp(exp_ts).strftime("%d/%m/%Y %H:%M:%S")
                return False, payload, f"Token expirado em {dt_exp}."
            if exp_ts - agora < 30:
                return False, payload, "Token expirando em menos de 30 segundos."
        except (ValueError, TypeError):
            pass
    return True, payload, clean


def obter_token_cache(matricula: str) -> tuple[Optional[str], Optional[dict]]:
    """Obtém token em cache se ainda for válido e não expirado."""
    matricula_clean = (matricula or "").strip()
    keys_to_check = [f"pap_token_{matricula_clean}"] if matricula_clean else []
    keys_to_check.append("pap_token_global")
    for k in keys_to_check:
        tok = _cache_get(k)
        if tok:
            ok, payload, clean = validar_e_decodificar_jwt(tok)
            if ok:
                return clean, payload
            _cache_delete(k)
    return None, None


def salvar_token_cache(matricula: str, token: str, exp_ts: Optional[float] = None) -> None:
    """Salva token no cache com TTL baseado na expiração do JWT (máx 2 horas)."""
    ok, payload, clean = validar_e_decodificar_jwt(token)
    if not ok:
        return
    agora = time.time()
    exp = exp_ts or (payload.get("exp") if payload else None)
    if exp:
        ttl = max(60, int(float(exp) - agora - 60))
        ttl = min(ttl, 7200)
    else:
        ttl = 3600
    matricula_clean = (matricula or "").strip()
    if matricula_clean:
        _cache_set(f"pap_token_{matricula_clean}", clean, ttl)
    _cache_set("pap_token_global", clean, ttl)


def remover_token_cache(matricula: str) -> None:
    matricula_clean = (matricula or "").strip()
    if matricula_clean:
        _cache_delete(f"pap_token_{matricula_clean}")
    _cache_delete("pap_token_global")


def verificar_cooldown_login(matricula: str) -> tuple[bool, int]:
    """Retorna (esta_em_cooldown, segundos_restantes)."""
    matricula_clean = (matricula or "").strip()
    keys = [f"pap_cooldown_{matricula_clean}"] if matricula_clean else []
    keys.append("pap_cooldown_global")
    agora = time.time()
    for k in keys:
        until = _cache_get(k)
        if until:
            try:
                until_f = float(until)
                if until_f > agora:
                    return True, int(until_f - agora)
            except (ValueError, TypeError):
                pass
            _cache_delete(k)
    return False, 0


def registrar_cooldown_login(matricula: str, segundos: int = 900) -> None:
    """Ativa cooldown de login para evitar bloqueio por tentativas automáticas seguidas."""
    matricula_clean = (matricula or "").strip()
    until = time.time() + segundos
    if matricula_clean:
        _cache_set(f"pap_cooldown_{matricula_clean}", until, segundos)
    _cache_set("pap_cooldown_global", until, segundos)
    logger.warning("[HISTORICO PAP] Cooldown de login ativado por %s segundos para matrícula %s", segundos, matricula_clean)


def limpar_cooldown_login(matricula: str) -> None:
    matricula_clean = (matricula or "").strip()
    if matricula_clean:
        _cache_delete(f"pap_cooldown_{matricula_clean}")
    _cache_delete("pap_cooldown_global")


def obter_status_sessao_pap(matricula: str) -> dict:
    """Status resumido para a UI: token ativo, cooldown e expiração."""
    tok, payload = obter_token_cache(matricula)
    em_cooldown, seg_cooldown = verificar_cooldown_login(matricula)
    exp_min = 0
    if payload and payload.get("exp"):
        try:
            exp_min = max(0, int((float(payload["exp"]) - time.time()) // 60))
        except Exception:
            exp_min = 0
    return {
        "tem_token_valido": bool(tok),
        "expira_em_minutos": exp_min,
        "cooldown_ativo": em_cooldown,
        "cooldown_restante_minutos": max(1, seg_cooldown // 60) if em_cooldown else 0,
        "matricula": (matricula or "").strip(),
    }


# Extrai Bearer JWT do cookie/localStorage varrendo todas as chaves e Redux persist.
JS_TOKEN = """
() => {
  const cleanJwt = (s) => {
    if (!s || typeof s !== 'string') return '';
    const m = s.match(/eyJ[A-Za-z0-9_\\-\\+\\/=]{5,}\\.[A-Za-z0-9_\\-\\+\\/=]{5,}\\.[A-Za-z0-9_\\-\\+\\/=]{5,}/);
    return m ? m[0] : '';
  };

  for (const store of [localStorage, sessionStorage]) {
    try {
      for (const key of ['token', 'accessToken', 'access_token', 'authToken', 'jwt', 'auth', 'user']) {
        const val = store.getItem(key);
        const jwt = cleanJwt(val);
        if (jwt) return jwt;
      }
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        const val = store.getItem(k);
        const jwt = cleanJwt(val);
        if (jwt) return jwt;

        if (val && (val.startsWith('{') || val.startsWith('['))) {
          const walk = (o, depth) => {
            if (!o || depth > 5) return '';
            if (typeof o === 'string') {
              const j = cleanJwt(o);
              if (j) return j;
              if (o.startsWith('{') || o.startsWith('[')) {
                try { return walk(JSON.parse(o), depth + 1); } catch (e) {}
              }
              return '';
            }
            if (typeof o === 'object') {
              for (const prop of Object.keys(o)) {
                const res = walk(o[prop], depth + 1);
                if (res) return res;
              }
            }
            return '';
          };
          try {
            const found = walk(JSON.parse(val), 0);
            if (found) return found;
          } catch (e) {}
        }
      }
    } catch (e) {}
  }

  try {
    const cookies = (document.cookie || '').split(';');
    for (const c of cookies) {
      const parts = c.trim().split('=');
      if (parts.length >= 2) {
        const val = decodeURIComponent(parts.slice(1).join('='));
        const jwt = cleanJwt(val);
        if (jwt) return jwt;
      }
    }
  } catch (e) {}

  return '';
}
"""

JS_FETCH = """
async (url) => {
  try {
    const raw = (document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('token=')) || '').slice(6);
    const headers = { Accept: 'application/json' };
    if (raw) {
      const t = decodeURIComponent(raw);
      headers.Authorization = t.startsWith('Bearer') ? t : ('Bearer ' + t);
    }
    const r = await fetch(url, { credentials: 'include', headers });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {
      return { ok: false, status: r.status, error: 'parse', preview: text.slice(0, 280) };
    }
    return { ok: r.ok, status: r.status, json };
  } catch (e) {
    return { ok: false, status: 0, error: String((e && e.message) || e || 'fetch_failed') };
  }
}
"""


def _extrair_token_cookies(page) -> str:
    """Extrai o cookie 'token' diretamente dos cookies do Playwright (imune ao path /administrativo)."""
    if not page:
        return ""
    try:
        cookies = page.context.cookies()
        for c in cookies:
            if c.get("name") == "token" and "pap.niointernet.com.br" in (c.get("domain") or ""):
                val = (c.get("value") or "").strip()
                clean = limpar_jwt(val)
                if clean:
                    return clean
    except Exception as exc:
        logger.warning("[HISTORICO PAP] Não foi possível ler cookies do contexto: %s", exc)
    return ""


def _extrair_token(page) -> str:
    if not page:
        return ""
    # 1. Tentar ler do cookie "token" gerenciado pelo Playwright (imune ao path /administrativo)
    c_tok = _extrair_token_cookies(page)
    if c_tok:
        return c_tok
    # 2. Tentar via JS evaluation em localStorage/sessionStorage/document.cookie
    try:
        raw = page.evaluate(JS_TOKEN)
        if raw:
            return limpar_jwt((raw or "").strip())
    except Exception as exc:
        logger.warning("[HISTORICO PAP] Não foi possível ler token da página: %s", exc)
    return ""


def _gerar_anti_replay_hash() -> str:
    """Replica _getAuthorizationToken da SPA: encodeObjectBase64Xor(JSON.stringify(new Date))."""
    from datetime import datetime, timezone as dt_tz

    key = "-5Hsrpt5gb93N5L9ePT2bBC9MI9ThLctvltkuoOqh2Q"
    agora = datetime.now(dt_tz.utc)
    # Equivalente a Date.toISOString() / JSON.stringify(new Date)
    iso = agora.strftime("%Y-%m-%dT%H:%M:%S.") + f"{agora.microsecond // 1000:03d}Z"
    plaintext = json.dumps(iso)  # inclui aspas, como JSON.stringify(new Date)
    encoded = bytearray()
    for i, char in enumerate(plaintext):
        encoded.append(ord(char) ^ ord(key[i % len(key)]))
    return base64.b64encode(bytes(encoded)).decode("ascii")


def _jwt_base_sem_hash(token: str) -> str:
    """Remove hash anti-replay (36) se presente; devolve JWT puro (~297)."""
    t = limpar_jwt(token) or (token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    parts = t.split(".")
    if len(parts) != 3:
        return t
    sig = parts[2]
    if len(sig) >= 43 + 36:
        return f"{parts[0]}.{parts[1]}.{sig[:-36]}"
    if len(sig) > 43:
        return f"{parts[0]}.{parts[1]}.{sig[:43]}"
    return t


def _headers_auth(token: str, *, regenerar_anti_replay: bool = True) -> dict[str, str]:
    """
    Monta headers como a SPA (_getAuthorizationToken):
    cookie.token + encodeObjectBase64Xor(JSON.stringify(new Date))
    SEM prefixo Bearer.
    """
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://pap.niointernet.com.br",
        "Referer": "https://pap.niointernet.com.br/administrativo/historico",
    }
    if not token:
        return headers

    base_jwt = _jwt_base_sem_hash(token)
    if regenerar_anti_replay:
        t = base_jwt + _gerar_anti_replay_hash()
        logger.info(
            "[HISTORICO PAP] Authorization fresco (jwt=%d + hash36 => %d, sem Bearer).",
            len(base_jwt),
            len(t),
        )
    else:
        # Só para diagnóstico: reutiliza token+hash capturado (pode já estar velho)
        t = limpar_jwt(token) or base_jwt
        logger.info(
            "[HISTORICO PAP] Authorization capturado sem regenerar (len=%d).",
            len(t),
        )

    headers["Authorization"] = t
    return headers


def _fetch_json_http(url: str, headers: dict[str, str]) -> dict:
    try:
        resp = requests.get(url, headers=headers, timeout=60)
        status = resp.status_code
        text = resp.text
        try:
            json_body = resp.json()
        except Exception:
            json_body = None
            try:
                json_body = json.loads(text)
            except Exception:
                return {
                    "ok": False,
                    "status": status,
                    "error": "parse",
                    "preview": (text or "")[:280],
                }
        if status in (401, 403):
            logger.warning("[HISTORICO PAP] API HTTP %s — preview=%s", status, (text or "")[:180].replace("\n", " "))
        return {
            "ok": 200 <= status < 300,
            "status": status,
            "json": json_body,
            "preview": (text or "")[:280],
        }
    except Exception as exc:
        return {"ok": False, "status": 0, "error": f"requests: {exc}"}


def _log_cookies_debug(page, dominio: str = "pap-api.niointernet.com.br") -> None:
    """Loga os cookies e Web Storage (local/session) presentes no contexto Playwright para diagnóstico."""
    if not page:
        return
    try:
        all_cookies = page.context.cookies()
        api_cookies = [c for c in all_cookies if dominio in (c.get("domain") or "")]
        front_cookies = [c for c in all_cookies if "pap.niointernet.com.br" in (c.get("domain") or "")]
        logger.warning(
            "[HISTORICO PAP][DEBUG] Cookies no contexto: total=%d pap-api=%d pap-front=%d",
            len(all_cookies), len(api_cookies), len(front_cookies),
        )
        for c in api_cookies:
            val = (c.get("value") or "")[:50]
            logger.warning(
                "[HISTORICO PAP][DEBUG] Cookie API: name=%s domain=%s path=%s val_inicio=%s",
                c.get("name"), c.get("domain"), c.get("path"), val,
            )
        for c in front_cookies:
            val = (c.get("value") or "")
            logger.warning(
                "[HISTORICO PAP][DEBUG] Cookie FRONT: name=%s domain=%s path=%s dots=%d len=%d val_inicio=%s val_fim=%s",
                c.get("name"), c.get("domain"), c.get("path"),
                val.count("."), len(val), val[:40], val[-20:],
            )
        
        # Dump Web Storage (LocalStorage e SessionStorage)
        try:
            ls_data = page.evaluate("() => { let d={}; for(let i=0; i<localStorage.length; i++) { let k=localStorage.key(i); d[k] = localStorage.getItem(k); } return d; }")
            ss_data = page.evaluate("() => { let d={}; for(let i=0; i<sessionStorage.length; i++) { let k=sessionStorage.key(i); d[k] = sessionStorage.getItem(k); } return d; }")
            
            logger.warning("[HISTORICO PAP][DEBUG] LocalStorage keys: %s", list(ls_data.keys()))
            for k, v in ls_data.items():
                if v and len(v) > 20:
                    logger.warning("[HISTORICO PAP][DEBUG] LocalStorage[%s] (len=%d): %s...%s", k, len(v), v[:40], v[-20:])
                else:
                    logger.warning("[HISTORICO PAP][DEBUG] LocalStorage[%s]: %s", k, v)
                    
            logger.warning("[HISTORICO PAP][DEBUG] SessionStorage keys: %s", list(ss_data.keys()))
        except Exception as exc_ws:
            logger.warning("[HISTORICO PAP][DEBUG] Falha ao ler Web Storage: %s", exc_ws)
            
    except Exception as exc:
        logger.warning("[HISTORICO PAP][DEBUG] Falha ao listar cookies: %s", exc)


def _fetch_json(page, url: str, token: str = "") -> dict:
    """
    Busca JSON da API do PAP.

    NÃO usar page.evaluate(fetch): o bundle.js da SPA intercepta window.fetch
    e as chamadas manuais falham com "Failed to fetch".

    Estratégia:
    1) Playwright APIRequestContext (fora do JS da página) com Authorization da SPA
    2) HTTP requests direto
    3) Fallback regenerando anti-replay (só se o token vier sem hash)
    """
    tok = (token or "").strip()
    if not tok and page:
        tok = limpar_jwt(_extrair_token(page))

    def _parse_playwright_resp(resp) -> dict:
        status = resp.status
        text = resp.text()
        try:
            json_body = resp.json()
        except Exception:
            try:
                json_body = json.loads(text)
            except Exception:
                json_body = None
        return {
            "ok": 200 <= status < 300,
            "status": status,
            "json": json_body,
            "preview": (text or "")[:280],
        }

    def _tentar(headers: dict[str, str], rotulo: str) -> dict:
        auth_len = len(headers.get("Authorization") or "")
        # 1) context.request — bypassa o fetch patchado da SPA
        if page:
            try:
                resp = page.context.request.get(url, headers=headers, timeout=45000)
                parsed = _parse_playwright_resp(resp)
                if parsed["ok"]:
                    logger.info("[HISTORICO PAP] context.request (%s) OK: %s", rotulo, parsed["status"])
                    return parsed
                logger.warning(
                    "[HISTORICO PAP] context.request (%s) %s auth_len=%d — preview=%s",
                    rotulo,
                    parsed["status"],
                    auth_len,
                    (parsed.get("preview") or "")[:180].replace("\n", " "),
                )
                if parsed["status"] not in (401, 403):
                    return parsed
            except Exception as exc:
                logger.warning("[HISTORICO PAP] context.request (%s) falhou (%s)", rotulo, exc)

        # 2) HTTP direto
        resp_http = _fetch_json_http(url, headers)
        if resp_http.get("ok"):
            logger.info("[HISTORICO PAP] HTTP direto (%s) OK", rotulo)
            return resp_http
        logger.warning(
            "[HISTORICO PAP] HTTP direto (%s) %s auth_len=%d — preview=%s",
            rotulo,
            resp_http.get("status"),
            auth_len,
            (resp_http.get("preview") or "")[:120],
        )
        return resp_http

    # Sempre regenera hash fresco (como _getAuthorizationToken da SPA)
    headers_fresh = _headers_auth(tok, regenerar_anti_replay=True)
    res = _tentar(headers_fresh, "hash-fresco")
    if res.get("ok"):
        return res

    # Diagnóstico: tenta o token capturado sem regenerar (pode já estar velho)
    headers_cap = _headers_auth(tok, regenerar_anti_replay=False)
    res2 = _tentar(headers_cap, "capturado-cru")
    if res2.get("ok"):
        return res2

    return res if res.get("status") else res2


def _navegar_ao_historico_spa(page) -> None:
    """Navega para o Histórico de Pedidos via menu lateral da SPA ou goto direto."""
    if not page:
        return
    url_atual = (page.url or "").lower()
    if "administrativo/historico" in url_atual:
        # Aguarda a página terminar de renderizar o React antes de continuar
        try:
            page.wait_for_selector('button#drawer-filter, button:has-text("Filtrar"), button:has-text("Buscar")', timeout=10000)
        except Exception:
            page.wait_for_timeout(3000)
        return
    # Tentar navegação suave pelo menu da SPA (como a Ana faz)
    try:
        btn_pedidos = page.query_selector('text="Pedidos"') or page.query_selector('div:has-text("Pedidos")')
        if btn_pedidos and btn_pedidos.is_visible():
            btn_pedidos.click()
            page.wait_for_timeout(800)
            btn_hist = page.query_selector('text="Histórico de Pedidos"') or page.query_selector('a[href*="historico"]')
            if btn_hist and btn_hist.is_visible():
                btn_hist.click()
                page.wait_for_timeout(2000)
                if "historico" in (page.url or "").lower():
                    logger.info("[HISTORICO PAP] Navegação ao Histórico via menu SPA concluída com sucesso!")
                    return
    except Exception as exc:
        logger.debug("[HISTORICO PAP] Navegação por menu SPA falhou (%s); usando goto direto", exc)

    # Fallback: goto direto
    try:
        page.goto(PAP_HISTORICO_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
    except Exception as exc:
        logger.warning("[HISTORICO PAP] goto histórico: %s", exc)


def _force_click(page, btn) -> None:
    try:
        btn.click(timeout=2000)
    except Exception:
        try:
            page.evaluate("el => el.click()", btn)
        except Exception:
            pass


def _datas_url_correspondem(url: str, data_inicio: date | None, data_fim: date | None) -> bool:
    """True se dataInicio/dataFim da query batem com o período pedido (YYYY-MM-DD)."""
    if not url or not data_inicio or not data_fim:
        return True
    from urllib.parse import parse_qs, unquote, urlparse

    qs = parse_qs(urlparse(url).query)
    ini_raw = unquote((qs.get("dataInicio") or [""])[0])
    fim_raw = unquote((qs.get("dataFim") or [""])[0])
    if not ini_raw or not fim_raw:
        return False
    return ini_raw[:10] == data_inicio.isoformat() and fim_raw[:10] == data_fim.isoformat()


def _rewritar_url_vendas_periodo(url: str, data_inicio: date, data_fim: date, *, limit: int = 200) -> str:
    """Troca só dataInicio/dataFim (e limit) na URL; mantém demais params da SPA."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    out: list[tuple[str, str]] = []
    seen_limit = False
    for k, v in pairs:
        lk = k.lower()
        if lk == "datainicio":
            out.append((k, _iso_inicio(data_inicio)))
        elif lk == "datafim":
            out.append((k, _iso_fim(data_fim)))
        elif lk == "limit":
            out.append((k, str(limit)))
            seen_limit = True
        else:
            out.append((k, v))
    if not seen_limit:
        out.append(("limit", str(limit)))
    return urlunparse(parsed._replace(query=urlencode(out)))


def _authorization_de_request(request) -> str:
    try:
        auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    except Exception:
        return ""
    auth = (auth or "").strip()
    return auth if auth and "eyJ" in auth else ""


def _coletar_via_auth_capturado(
    page,
    *,
    authorization: str,
    data_inicio: date,
    data_fim: date,
    tipo_api: str = "VENDA",
    limit: int = 200,
) -> list[dict]:
    """
    Reconsulta /vendas com o Authorization EXATO da SPA (JWT + hash anti-replay).

    Não regenera o hash: forjar Authorization novo causa 401 jwt malformed.
    """
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    if not auth or not page:
        return []

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://pap.niointernet.com.br",
        "Referer": "https://pap.niointernet.com.br/administrativo/historico",
        "Authorization": auth,
    }
    data_ini = _iso_inicio(data_inicio)
    data_fim_s = _iso_fim(data_fim)
    packs: list[dict] = []
    total = None
    for page_n in range(1, 51):
        url = montar_url_vendas(
            data_inicio=data_ini,
            data_fim=data_fim_s,
            pdv="",
            tipo_api=tipo_api,
            page=page_n,
            limit=limit,
            status=STATUS_LISTA_PADRAO if tipo_api == "VENDA" else None,
        )
        try:
            resp = page.context.request.get(url, headers=headers, timeout=45000)
            status = resp.status
            try:
                body = resp.json()
            except Exception:
                body = None
            if status < 200 or status >= 300 or body is None:
                preview = ""
                try:
                    preview = (resp.text() or "")[:160]
                except Exception:
                    pass
                logger.warning(
                    "[HISTORICO PAP] Reconsulta período via auth SPA falhou page=%s HTTP %s — %s",
                    page_n,
                    status,
                    preview.replace("\n", " "),
                )
                break
            packs.append({"url": url, "status": status, "json": body})
            lista, total_api = extrair_lista_api(body)
            if total is None:
                total = total_api
            n = len(lista or [])
            logger.info(
                "[HISTORICO PAP] Reconsulta auth SPA page=%s: +%d itens (total=%s) período=%s→%s",
                page_n,
                n,
                total,
                data_inicio,
                data_fim,
            )
            if not lista:
                break
            if total is not None and page_n * limit >= int(total):
                break
            if n < limit:
                break
            time.sleep(0.35)
        except Exception as exc:
            logger.warning("[HISTORICO PAP] Reconsulta auth SPA erro: %s", exc)
            break
    return packs


def _preencher_datas_filtro_spa(page, data_inicio: date | None = None, data_fim: date | None = None) -> None:
    """Preenche datas no filtro do Histórico (React/Ant) para a SPA disparar /vendas.

    Não depende só de offsetParent (drawer Ant às vezes esconde inputs).
    O período pedido ainda é garantido via route.fetch na interceptação de /vendas.
    """
    if not page:
        return
    ini = data_inicio or date.today()
    fim = data_fim or date.today()
    ini_br = ini.strftime("%d/%m/%Y")
    fim_br = fim.strftime("%d/%m/%Y")
    try:
        preenchidos = page.evaluate(
            """([iniIso, fimIso, iniBr, fimBr]) => {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                const roots = [
                    ...document.querySelectorAll('.ant-drawer-open, .ant-drawer-content-wrapper, .ant-modal-open, [class*="drawer"]'),
                    document.body,
                ];
                const seen = new Set();
                const inputs = [];
                roots.forEach((root) => {
                    root.querySelectorAll('input').forEach((i) => {
                        if (seen.has(i)) return;
                        seen.add(i);
                        inputs.push(i);
                    });
                });
                let count = 0;
                const vals = [];
                inputs.forEach((i) => {
                    if (i.disabled) return;
                    const ph = (i.placeholder || '').toLowerCase();
                    const name = ((i.name || '') + ' ' + (i.id || '') + ' ' + (i.className || '')).toLowerCase();
                    const inPicker = !!(i.closest && i.closest('.ant-picker'));
                    const isData = i.type === 'date' || inPicker || ph.includes('data') || ph.includes('início')
                        || ph.includes('inicio') || ph.includes('fim') || name.includes('data')
                        || name.includes('date') || name.includes('picker');
                    if (!isData && i.type !== 'text') return;
                    // No drawer, preenche pickers/datas; fora do drawer só type=date
                    if (!isData && !i.closest('.ant-drawer-open') && !i.closest('.ant-picker')) return;
                    let val;
                    if (i.type === 'date') {
                        val = (count === 0) ? iniIso : fimIso;
                    } else if (ph.includes('fim') || name.includes('fim') || name.includes('end') || count > 0) {
                        val = fimBr;
                    } else {
                        val = iniBr;
                    }
                    const tracker = i._valueTracker;
                    if (tracker) tracker.setValue('');
                    setter.call(i, val);
                    i.dispatchEvent(new Event('input', { bubbles: true }));
                    i.dispatchEvent(new Event('change', { bubbles: true }));
                    i.dispatchEvent(new Event('blur', { bubbles: true }));
                    vals.push(val);
                    count += 1;
                });
                return { count, vals };
            }""",
            [ini.isoformat(), fim.isoformat(), ini_br, fim_br],
        )
        logger.info(
            "[HISTORICO PAP] Datas filtro SPA pedidas %s→%s; inputs tocados=%s",
            ini,
            fim,
            preenchidos,
        )
        page.wait_for_timeout(400)
    except Exception as exc:
        logger.debug("[HISTORICO PAP] Falha ao preencher datas SPA (JS): %s", exc)

    # Reforço via digitação Playwright (Ant Design RangePicker)
    try:
        loc = page.locator(
            '.ant-drawer-open .ant-picker-input input, .ant-picker-input input, '
            'input[placeholder*="Data" i], input[placeholder*="data"], input[type="date"]'
        )
        n = loc.count()
        for idx in range(min(n, 4)):
            item = loc.nth(idx)
            try:
                tipo = (item.get_attribute("type") or "").lower()
                val = ini.isoformat() if tipo == "date" else (ini_br if idx % 2 == 0 else fim_br)
                if tipo == "date" and idx % 2 == 1:
                    val = fim.isoformat()
                item.click(timeout=1500, force=True)
                item.fill(val, force=True)
                item.press("Enter")
                item.press("Tab")
            except Exception:
                continue
        page.wait_for_timeout(300)
    except Exception as exc:
        logger.debug("[HISTORICO PAP] Falha ao digitar datas SPA: %s", exc)


def _tentar_clicar_filtrar(
    page,
    *,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> None:
    """Clica Filtrar/Buscar na SPA para ela mesma chamar /api/portal/vendas."""
    if not page:
        return

    seletores_filtro = [
        'button:has-text("Filtrar")',
        'button:has-text("Buscar")',
        'button:has-text("FILTRAR")',
        'button:has-text("BUSCAR")',
        'button[class*="filtrar"]',
        'button.btn-filters-new',
        'button:has-text("Pesquisar")',
        'button:has-text("Aplicar")',
    ]
    seletores_abrir_filtro = [
        'button#drawer-filter',
        '#drawer-filter',
        'button:has-text("Filtros")',
        'span:has-text("Filtros")',
        'div:has-text("Filtro")',
    ]

    for sel in seletores_abrir_filtro:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                logger.info("[HISTORICO PAP] Abrindo filtros com '%s'...", sel)
                _force_click(page, btn)
                page.wait_for_timeout(2000)
                try:
                    page.wait_for_selector(
                        '.ant-picker-input input, input[placeholder*="Data" i], input[type="date"], .ant-drawer-open input',
                        timeout=4000,
                    )
                except Exception:
                    pass
                break
        except Exception:
            pass

    _preencher_datas_filtro_spa(page, data_inicio, data_fim)

    for sel in seletores_filtro:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                logger.info("[HISTORICO PAP] Clicando em '%s' para disparar XHR da SPA...", sel)
                _force_click(page, btn)
                page.wait_for_timeout(2500)
                # Segundo clique (algumas UIs Ant só disparam no 2º)
                try:
                    if btn.is_visible():
                        _force_click(page, btn)
                        page.wait_for_timeout(1500)
                except Exception:
                    pass
                return
        except Exception:
            pass

    # Sem drawer: tenta qualquer botão Filtrar/Buscar na página
    try:
        btn = page.get_by_role("button", name=re.compile(r"filtrar|buscar|pesquisar|aplicar", re.I)).first
        if btn:
            logger.info("[HISTORICO PAP] Clicando botão Filtrar via role...")
            btn.click(timeout=3000, force=True)
            page.wait_for_timeout(2500)
            return
    except Exception:
        pass

    logger.warning("[HISTORICO PAP] Não encontrou botão de filtrar/buscar na página histórico.")


def _url_eh_api_pap(url: str) -> bool:
    return "pap-api.niointernet.com.br" in ((url or "").lower())


def _url_eh_vendas_pap(url: str) -> bool:
    u = (url or "").lower()
    return _url_eh_api_pap(u) and "/api/portal/vendas" in u


def _coletar_vendas_via_rede_spa(
    page,
    *,
    data_inicio: date | None = None,
    data_fim: date | None = None,
    timeout_ms: int = 50000,
    max_paginas_ui: int = 8,
) -> list[dict]:
    """
    Captura /api/portal/vendas pela rede da SPA (modo seguro).

    1) Intercepta o XHR da própria SPA com route.fetch (headers/Authorization frescos).
    2) Se o período divergir, reescreve só dataInicio/dataFim na URL do fetch.
    3) Nunca reutiliza Authorization fora do request original (anti-replay → jwt malformed).
    """
    if not page:
        return []

    collected: list[dict] = []
    erros: list[str] = []
    captured_auth = {"value": ""}
    seen_urls: set[str] = set()

    def _matched() -> list[dict]:
        if not data_inicio or not data_fim:
            return list(collected)
        return [p for p in collected if _datas_url_correspondem(p.get("url") or "", data_inicio, data_fim)]

    def _append_pack(url: str, status: int, body: dict) -> None:
        key = f"{status}|{(url or '')[:400]}"
        if key in seen_urls:
            return
        seen_urls.add(key)
        collected.append({"url": url, "status": status, "json": body})

    def _on_request(request):
        try:
            if not _url_eh_api_pap(request.url):
                return
            # Diagnóstico: o que a SPA está chamando (ajuda quando /vendas não dispara)
            u = request.url or ""
            if "/api/portal/" in u.lower():
                logger.info(
                    "[HISTORICO PAP] SPA→pap-api %s %s",
                    request.method,
                    u[:180],
                )
            auth = _authorization_de_request(request)
            if auth:
                captured_auth["value"] = auth
        except Exception:
            pass

    page.on("request", _on_request)

    def _on_route(route):
        """
        Intercepta /vendas da SPA e refaz o request com route.fetch.

        Importante: NÃO usar route.continue_(url=...). Trocar a URL no continue_
        costuma perder/alterar Authorization em cross-origin → SPA não recebe
        /vendas e o fallback com auth reutilizado cai em jwt malformed (anti-replay).

        route.fetch reutiliza os headers EXATOS do request da SPA (token fresco).
        """
        req = route.request
        method = (req.method or "").upper()
        if method != "GET" or not _url_eh_vendas_pap(req.url):
            try:
                route.continue_()
            except Exception:
                pass
            return

        final_url = req.url
        rewrote = False
        if data_inicio and data_fim and not _datas_url_correspondem(req.url, data_inicio, data_fim):
            final_url = _rewritar_url_vendas_periodo(req.url, data_inicio, data_fim)
            rewrote = True
            logger.info(
                "[HISTORICO PAP] route.fetch reescrevendo período /vendas → %s→%s",
                data_inicio,
                data_fim,
            )

        try:
            api_resp = route.fetch(url=final_url) if rewrote else route.fetch()
            status = api_resp.status
            body = None
            preview = ""
            try:
                body = api_resp.json()
            except Exception:
                try:
                    text = api_resp.text()
                    preview = (text or "")[:180]
                    body = json.loads(text) if text else None
                except Exception:
                    body = None

            if status < 200 or status >= 300:
                erros.append(f"HTTP {status}: {preview}")
                logger.warning(
                    "[HISTORICO PAP] route.fetch /vendas %s — preview=%s",
                    status,
                    (preview or "").replace("\n", " "),
                )
            elif body is not None:
                _append_pack(final_url, status, body)
                logger.info(
                    "[HISTORICO PAP] Capturado /vendas via route.fetch (status=%s, rewrite=%s)",
                    status,
                    rewrote,
                )

            route.fulfill(response=api_resp)
            return
        except Exception as exc:
            logger.warning(
                "[HISTORICO PAP] route.fetch /vendas falhou: %s — tentando continue_ com headers",
                exc,
            )
            try:
                headers = dict(req.headers)
                if rewrote:
                    route.continue_(url=final_url, headers=headers)
                else:
                    route.continue_(headers=headers)
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    try:
                        route.abort()
                    except Exception:
                        pass

    route_installed = False
    try:
        # Sempre intercepta /vendas: captura com headers da SPA + ajusta período se preciso
        page.route("**/api/portal/vendas**", _on_route)
        route_installed = True

        _navegar_ao_historico_spa(page)

        # 1) Espera auto-load da SPA
        fim_load = time.time() + min(12.0, timeout_ms / 1000.0)
        while time.time() < fim_load and not collected and not captured_auth["value"]:
            page.wait_for_timeout(400)

        matched = _matched()
        if matched:
            return matched
        # Se veio /vendas (mesmo fora do período) e o rewrite falhou, ainda devolve o capturado
        if collected and not matched and not (data_inicio and data_fim):
            return list(collected)

        # 2) Tenta Filtrar na UI — o route.fetch ajusta o período no request da SPA
        _tentar_clicar_filtrar(page, data_inicio=data_inicio, data_fim=data_fim)
        fim = time.time() + min(25.0, timeout_ms / 1000.0)
        while time.time() < fim and not _matched():
            if collected:
                # rewrite pode ter falhado; se já temos pacotes no período, sai
                break
            page.wait_for_timeout(400)

        matched = _matched()
        if not matched and collected:
            # Aceita o que a SPA trouxe (melhor que zero) se rewrite não casou período
            logger.warning(
                "[HISTORICO PAP] /vendas capturado fora do período pedido — usando pacotes da SPA."
            )
            matched = list(collected)

        if matched:
            for _ in range(max(0, max_paginas_ui - 1)):
                nxt = None
                for sel in (
                    'button:has-text("Próximo")',
                    'button:has-text("Proximo")',
                    'li.ant-pagination-next:not(.ant-pagination-disabled) button',
                    'button[aria-label="next"]',
                    'button[aria-label="Próxima página"]',
                    ".pagination button:has-text(\">\")",
                ):
                    try:
                        cand = page.query_selector(sel)
                        if cand and cand.is_visible() and cand.is_enabled():
                            nxt = cand
                            break
                    except Exception:
                        continue
                if not nxt:
                    break
                antes = len(_matched() or matched)
                try:
                    _force_click(page, nxt)
                except Exception:
                    break
                page.wait_for_timeout(2000)
                agora = _matched() or list(collected)
                if len(agora) == antes:
                    page.wait_for_timeout(2000)
                if len(_matched() or list(collected)) == antes:
                    break
            return _matched() or list(collected)

        # 3) Último recurso: NÃO reutilizar Authorization (anti-replay one-shot → jwt malformed).
        # Só loga diagnóstico.
        if captured_auth["value"]:
            logger.warning(
                "[HISTORICO PAP] Havia Authorization da SPA (len=%d) mas /vendas não disparou. "
                "Não reutilizamos o token (anti-replay). Auth events ok; falta XHR /vendas.",
                len(captured_auth["value"]),
            )
        if not collected and erros:
            logger.warning(
                "[HISTORICO PAP] Rede SPA sem sucesso em /vendas. Último erro: %s",
                erros[-1][:200],
            )
        return _matched()
    finally:
        if route_installed:
            try:
                page.unroute("**/api/portal/vendas**", _on_route)
            except Exception:
                try:
                    page.unroute("**/api/portal/vendas**")
                except Exception:
                    pass
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass


def _run_django_sync(func, timeout_seconds: int = 120):
    import queue

    import django.db

    q = queue.Queue()

    def worker():
        try:
            django.db.close_old_connections()
            q.put(("ok", func()))
        except Exception as e:
            q.put(("err", e))
        finally:
            django.db.close_old_connections()

    t = threading.Thread(target=worker, daemon=True, name="hist-pap-orm")
    t.start()
    t.join(timeout=timeout_seconds)
    if not q.empty():
        kind, payload = q.get()
        if kind == "err":
            raise payload
        return payload
    raise TimeoutError("django_sync_timeout")


def _intervalo() -> float:
    lo = float(getattr(settings, "HISTORICO_PAP_INTERVALO_MIN_SEG", 4))
    hi = float(getattr(settings, "HISTORICO_PAP_INTERVALO_MAX_SEG", 6))
    if hi < lo:
        hi = lo
    return random.uniform(lo, hi)


def _validar_credenciais(usuario) -> Tuple[bool, str]:
    matricula = (getattr(usuario, "matricula_pap", None) or "").strip()
    senha = (getattr(usuario, "senha_pap", None) or "").strip()
    if not matricula or not senha:
        return False, (
            "O login Diretoria selecionado não tem matrícula/senha PAP. "
            "Cadastre na Governança antes de buscar o histórico."
        )
    return True, matricula


def busca_em_andamento():
    from crm_app.models import HistoricoPapBusca

    return (
        HistoricoPapBusca.objects.filter(
            status__in=[
                HistoricoPapBusca.STATUS_PENDENTE,
                HistoricoPapBusca.STATUS_EM_ANDAMENTO,
            ]
        )
        .select_related("login_pap")
        .order_by("-iniciado_em")
        .first()
    )


def registrar_exportacao(usuario, nome: str, content: bytes) -> dict:
    from crm_app.models import HistoricoPapPedido

    pares = parse_arquivo_exportacao(nome, content)
    if not pares:
        raise ValueError("Não achei a coluna Pedido (protocolo) neste arquivo.")

    conhecidos = set(
        HistoricoPapPedido.objects.filter(
            numero_pedido__in=[p[0] for p in pares]
        ).values_list("numero_pedido", flat=True)
    )
    novos = 0
    objs = []
    for ped, tipo, payload in pares:
        if ped in conhecidos:
            continue
        conhecidos.add(ped)
        objs.append(
            HistoricoPapPedido(
                numero_pedido=ped,
                tipo_venda=tipo or HistoricoPapPedido.TIPO_VENDA,
                origem="exportacao",
                payload=payload if isinstance(payload, dict) else {"numeroPedido": ped},
                pdv="",
            )
        )
        novos += 1
        if len(objs) >= 500:
            HistoricoPapPedido.objects.bulk_create(objs, ignore_conflicts=True)
            objs = []
    if objs:
        HistoricoPapPedido.objects.bulk_create(objs, ignore_conflicts=True)
    return {
        "lidos": len(pares),
        "novos": novos,
        "ja_existiam": len(pares) - novos,
        "total_base": HistoricoPapPedido.objects.count(),
        "grava_venda": False,
    }


def serializar_busca(busca, *, em_andamento: bool) -> dict:
    login_user = getattr(busca, "login_pap", None)
    return {
        "id": busca.id,
        "status": busca.status,
        "em_andamento": em_andamento,
        "data_inicio": busca.data_inicio.isoformat() if busca.data_inicio else "",
        "data_fim": busca.data_fim.isoformat() if busca.data_fim else "",
        "pdv": busca.pdv or "",
        "tipos": busca.tipos or [],
        "encontrados": busca.encontrados,
        "novos": busca.novos,
        "ignorados": busca.ignorados,
        "por_tipo": busca.por_tipo or {},
        "mensagem": busca.mensagem or "",
        "grava_venda": False,
        "login_pap": getattr(login_user, "username", None) or "",
        "iniciado_em": busca.iniciado_em.isoformat() if busca.iniciado_em else "",
        "finalizado_em": busca.finalizado_em.isoformat() if busca.finalizado_em else "",
    }


def criar_e_iniciar_busca(
    usuario,
    *,
    data_inicio: date,
    data_fim: date,
    pdv: str,
    tipos: list[str],
    token_manual: str = "",
):
    from django.db import transaction

    from crm_app.models import HistoricoPapBusca
    from crm_app.pool_historico_pap import obter_login_historico_pap

    if data_fim < data_inicio:
        return None, "Data fim anterior à data início."
    if (data_fim - data_inicio).days > MAX_DIAS_BUSCA:
        return None, f"O intervalo máximo é {MAX_DIAS_BUSCA} dias."

    tipos_ok = tipos_solicitados(tipos)
    pdv = (pdv or "").strip()
    token_manual = (token_manual or "").strip()

    with transaction.atomic():
        login_pap, err_pool = obter_login_historico_pap()
        if err_pool:
            return None, err_pool

        ok, msg = _validar_credenciais(login_pap)
        if not ok and not token_manual:
            return None, msg

        busca = HistoricoPapBusca.objects.create(
            usuario=usuario,
            login_pap=login_pap,
            status=HistoricoPapBusca.STATUS_EM_ANDAMENTO,
            data_inicio=data_inicio,
            data_fim=data_fim,
            pdv=pdv,
            tipos=tipos_ok,
            mensagem=f"Usando login Diretoria: {login_pap.username}" + (" (Token manual)" if token_manual else ""),
            relatorio_json={"fase": "iniciando", "login_pap": login_pap.username, "token_manual": bool(token_manual)},
        )
        login_id = login_pap.id
        busca_id = busca.id

    t = threading.Thread(
        target=_runner,
        args=(busca_id, login_id, token_manual),
        name=f"hist-pap-{busca_id}",
        daemon=True,
    )
    t.start()
    return busca_id, None


def xlsx_novos_da_busca(busca_id: int) -> tuple[bytes, str]:
    from crm_app.models import HistoricoPapBusca, HistoricoPapPedido

    busca = HistoricoPapBusca.objects.get(pk=busca_id)
    numeros = [normalizar_pedido(n) for n in (busca.novos_numeros or [])]
    numeros = [n for n in numeros if n]
    linhas = []
    if numeros:
        qs = HistoricoPapPedido.objects.filter(numero_pedido__in=numeros)
        by_num = {p.numero_pedido: p for p in qs}
        for n in numeros:
            p = by_num.get(n)
            if not p:
                continue
            if p.payload:
                linhas.append(map_pedido_api(p.payload, p.tipo_venda))
            else:
                linhas.append({"tipo_venda": p.tipo_venda, "pedido": p.numero_pedido, "status": p.status})
    nome = f"Historico_PAP_{busca.data_inicio}_{busca.data_fim}.xlsx"
    return montar_xlsx_historico(linhas), nome


def _atualizar(busca_id: int, **kwargs):
    from crm_app.models import HistoricoPapBusca

    HistoricoPapBusca.objects.filter(pk=busca_id).update(**kwargs)


def _runner(busca_id: int, login_pap_id: int, token_manual: str = ""):
    import django.db

    django.db.close_old_connections()
    try:
        _executar_busca(busca_id, login_pap_id, token_manual=token_manual)
    except Exception as exc:
        logger.exception("[HISTORICO PAP] Falha no job %s", busca_id)
        msg = f"Falha ao buscar o histórico PAP: {exc}"[:500]
        try:
            _run_django_sync(
                lambda: _atualizar(
                    busca_id,
                    status="erro",
                    mensagem=msg,
                    finalizado_em=timezone.now(),
                )
            )
        except Exception:
            logger.exception("[HISTORICO PAP] Nem o status de erro pôde ser gravado.")
    finally:
        django.db.close_old_connections()


def _iso_inicio(d: date) -> str:
    return f"{d.isoformat()}T00:00:00-03:00"


def _iso_fim(d: date) -> str:
    return f"{d.isoformat()}T23:59:59-03:00"


def _pedido_conhecido(numero: str) -> bool:
    from crm_app.models import HistoricoPapPedido

    return HistoricoPapPedido.objects.filter(numero_pedido=numero).exists()


def _salvar_novo(numero: str, tipo: str, pdv: str, payload: dict) -> bool:
    from crm_app.models import HistoricoPapPedido

    if not numero:
        return False
        
    existente = HistoricoPapPedido.objects.filter(numero_pedido=numero).first()
    if existente:
        if existente.tipo_venda != tipo:
            existente.tipo_venda = tipo
            existente.save(update_fields=['tipo_venda'])
        return False
        
    data_criacao = None
    raw = payload.get("dataCriacao")
    if raw:
        try:
            data_criacao = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            data_criacao = None
    HistoricoPapPedido.objects.create(
        numero_pedido=numero,
        tipo_venda=tipo,
        pdv=pdv or "",
        status=str(payload.get("status") or payload.get("chaveStatusPrimario") or "")[:80],
        data_criacao_pap=data_criacao,
        origem="api",
        payload=payload,
    )
    return True





def _classificar_tipo_item(item: dict, tipos_filtro: list[str] | None) -> str | None:
    """Mapeia um item da API para TIPO_* do modelo, respeitando filtro da busca."""
    from crm_app.models import HistoricoPapPedido
    from crm_app.historico_pap import normalizar_tipo

    raw = (
        item.get("tipoVenda")
        or item.get("tipo_venda")
        or item.get("tipo")
        or ""
    )
    tipo = normalizar_tipo(raw) if raw else ""
    if not tipo:
        # Histórico padrão costuma ser VENDA quando o filtro da SPA é VENDA
        tipo = "VENDA"
    permitidos = tipos_filtro or ["VENDA", "INTERESSE", "PRE_VENDA"]
    if tipo not in permitidos:
        return None
    return getattr(HistoricoPapPedido, f"TIPO_{tipo.replace('-', '_')}", tipo)


def _executar_loop_busca(page, *, busca_id: int, busca) -> tuple[bool, str]:
    """
    Coleta o relatório do Histórico PAP.

    Estratégia principal: deixar a SPA chamar /api/portal/vendas e capturar
    a resposta na rede do Playwright (sem forjar Authorization).

    Motivo: context.request/requests com JWT+hash (mesmo fresco) recebem
    401 jwt malformed; a SPA autenticada consegue.

    Fallback: API HTTP direta (legado) — só se a rede SPA não trouxer nada.
    Alternativa operacional: upload da exportação PAP (registrar_exportacao).
    """
    from crm_app.models import HistoricoPapBusca, HistoricoPapPedido
    from crm_app.historico_pap import normalizar_pedido, extrair_lista_api, montar_url_vendas, TIPO_API_ALIASES
    from django.utils import timezone

    encontrados = 0
    novos = 0
    ignorados = 0
    novos_numeros = []
    por_tipo = {}
    vendas_para_processar = []
    origem_coleta = "spa_rede"

    data_ini = _iso_inicio(busca.data_inicio)
    data_fim = _iso_fim(busca.data_fim)
    pdv = busca.pdv
    tipos = list(busca.tipos or [])

    # ─── PASSO 1: Capturar /vendas da própria SPA ───────────────────────────────
    logger.info(
        "[HISTORICO PAP] Coleta via rede da SPA (sem forjar token). Tipos=%s período=%s→%s",
        tipos or ["VENDA", "INTERESSE", "PRE_VENDA"],
        data_ini[:10],
        data_fim[:10],
    )
    respostas_spa = _coletar_vendas_via_rede_spa(
        page,
        data_inicio=busca.data_inicio,
        data_fim=busca.data_fim,
        timeout_ms=55000,
        max_paginas_ui=12,
    )

    itens_brutos: list[dict] = []
    spa_ok = False
    if respostas_spa:
        spa_ok = True
        for pack in respostas_spa:
            lista, total = extrair_lista_api(pack.get("json"))
            lista = lista or []
            itens_brutos.extend([x for x in lista if isinstance(x, dict)])
            logger.info(
                "[HISTORICO PAP] Pacote SPA /vendas: +%d itens (total API=%s)",
                len(lista),
                total,
            )

    # ─── PASSO 2: Fallback seguro — reload + Filtrar de novo (sem reusar Authorization) ─
    if not spa_ok:
        origem_coleta = "spa_rede_retry"
        logger.warning(
            "[HISTORICO PAP] Rede SPA sem pacote /vendas — retry: reload histórico + Filtrar "
            "(sem reutilizar Authorization)."
        )
        try:
            page.goto(PAP_HISTORICO_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
        except Exception as exc:
            logger.warning("[HISTORICO PAP] goto histórico (retry): %s", exc)

        respostas_spa = _coletar_vendas_via_rede_spa(
            page,
            data_inicio=busca.data_inicio,
            data_fim=busca.data_fim,
            timeout_ms=55000,
            max_paginas_ui=12,
        )
        if respostas_spa:
            spa_ok = True
            for pack in respostas_spa:
                lista, total = extrair_lista_api(pack.get("json"))
                lista = lista or []
                itens_brutos.extend([x for x in lista if isinstance(x, dict)])
                logger.info(
                    "[HISTORICO PAP] Pacote SPA /vendas (retry): +%d itens (total API=%s)",
                    len(lista),
                    total,
                )

        if not spa_ok:
            return False, (
                "Nenhum pedido obtido: a SPA não disparou /api/portal/vendas no Histórico. "
                "Alternativa estável: exportar no PAP e enviar o arquivo no site "
                "(registrar exportação)."
            )

    vistos = set()
    for v in itens_brutos:
        ped = normalizar_pedido(v.get("numeroPedido"))
        if not ped or ped in vistos:
            continue
        t_api = _classificar_tipo_item(v, tipos or None)
        if not t_api:
            continue
        pdv_venda = str(v.get("identificadorPdv") or "").strip()
        if pdv and pdv_venda != pdv:
            continue
        vistos.add(ped)
        vendas_para_processar.append((ped, t_api, pdv_venda, v))

    # ─── PASSO 3: Gravar no banco ────────────────────────────────────────────────
    def _processar_banco():
        nonlocal encontrados, ignorados, novos, por_tipo, novos_numeros
        for ped, t_api, pdv_venda, v in vendas_para_processar:
            encontrados += 1
            if t_api not in por_tipo:
                por_tipo[t_api] = {"encontrados": 0, "novos": 0, "ignorados": 0}
            por_tipo[t_api]["encontrados"] += 1
            if _pedido_conhecido(ped):
                ignorados += 1
                por_tipo[t_api]["ignorados"] += 1
            else:
                if _salvar_novo(ped, t_api, pdv_venda, v):
                    novos += 1
                    por_tipo[t_api]["novos"] += 1
                    novos_numeros.append(ped)
                else:
                    ignorados += 1
                    por_tipo[t_api]["ignorados"] += 1

    _run_django_sync(_processar_banco)

    status_final = (
        HistoricoPapBusca.STATUS_CANCELADO
        if _job_cancelado(busca_id)
        else HistoricoPapBusca.STATUS_CONCLUIDO
    )
    msg = (
        "Busca efetuada via rede da SPA (resposta de /vendas capturada)."
        if origem_coleta == "spa_rede"
        else "Busca efetuada com Authorization capturado da SPA (reconsulta de período)."
    )
    _run_django_sync(
        lambda: _atualizar(
            busca_id,
            status=status_final,
            encontrados=encontrados,
            novos=novos,
            ignorados=ignorados,
            por_tipo=por_tipo,
            novos_numeros=novos_numeros,
            mensagem=msg,
            finalizado_em=timezone.now(),
            relatorio_json={"fase": "concluido", "origem": origem_coleta, "por_tipo": por_tipo},
        )
    )
    return True, ""





def _executar_busca(busca_id: int, login_pap_id: int, token_manual: str = ""):
    """
    Fluxo alinhado ao site-record (Bearer + cookies-first + API direta),
    com reúso de sessão para reduzir logins na Nio.

    Só invalida storage state se a API rejeitar o token (401/invalid signature),
    e nesse caso faz no máximo 1 re-login fresco.
    """
    from django.contrib.auth import get_user_model
    from crm_app.models import HistoricoPapBusca
    from crm_app.services_pap_nio import PAPNioAutomation
    import os
    from django.conf import settings
    from django.utils import timezone

    User = get_user_model()
    login_pap = _run_django_sync(lambda: User.objects.get(pk=login_pap_id))
    busca = _run_django_sync(lambda: HistoricoPapBusca.objects.get(pk=busca_id))

    matricula = (getattr(login_pap, "matricula_pap", None) or "").strip()
    senha = (getattr(login_pap, "senha_pap", None) or "").strip()

    def _marcar_erro(mensagem: str) -> None:
        _run_django_sync(
            lambda: _atualizar(
                busca_id,
                status=HistoricoPapBusca.STATUS_ERRO,
                mensagem=(mensagem or "")[:500],
                finalizado_em=timezone.now(),
            )
        )

    def _token_rejeitado(err_msg: str) -> bool:
        low = (err_msg or "").lower()
        # jwt malformed ao forjar hash é bug nosso — NÃO é sessão morta; cooldown queima o Igor.
        if "jwt malformed" in low or "forjar authorization" in low:
            return False
        return (
            "401" in low
            or "403" in low
            or "invalid signature" in low
            or "sessão/token rejeitado" in low
            or "token da sessão pap inválido" in low
            or "não foi possível extrair o token" in low
        )

    em_cooldown, seg_cooldown = verificar_cooldown_login(matricula)
    if em_cooldown:
        min_restantes = max(1, seg_cooldown // 60)
        _marcar_erro(
            f"Cooldown de segurança ativo ({min_restantes} min restantes) para proteger "
            f"o usuário {login_pap.username} contra bloqueios de login na Nio."
        )
        return

    automacao = PAPNioAutomation(
        matricula_pap=matricula,
        senha_pap=senha,
        vendedor_nome=getattr(login_pap, "username", "Historico-PAP") or "Historico-PAP",
        headless=getattr(settings, "PAP_HEADLESS", True),
        capture_screenshots=False,
        optimize_for_credit=False,
        url_pos_login=PAP_HISTORICO_URL,
    )

    def _rodar_com_sessao(*, forcar_login_fresco: bool) -> tuple[bool, str]:
        if forcar_login_fresco and hasattr(automacao, "storage_state_path"):
            path = automacao.storage_state_path
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    logger.warning(
                        "[HISTORICO PAP] Storage state invalidado (token rejeitado). "
                        "Um único re-login fresco será tentado."
                    )
                except Exception:
                    pass

        ok, msg = automacao.iniciar_sessao()
        if not ok:
            return False, msg or "Falha ao logar no PAP."

        limpar_cooldown_login(matricula)
        return _executar_loop_busca(
            page=automacao.page,
            busca_id=busca_id,
            busca=busca,
        )

    try:
        # Apenas 1 tentativa com sessão reutilizada (sem segundo login automático).
        # jwt malformed NÃO se resolve com re-login e queima tentativas na Nio.
        sucesso, err_msg = _rodar_com_sessao(forcar_login_fresco=False)

        if sucesso:
            return

        if "Falha ao logar" in (err_msg or ""):
            registrar_cooldown_login(matricula, 900)
            _marcar_erro((err_msg or "Falha ao logar no PAP.") + " Cooldown anti-bloqueio ativado.")
            return

        if _token_rejeitado(err_msg):
            remover_token_cache(matricula)
            # Cooldown longo: protege login Igor; use token manual no Funil se precisar testar.
            registrar_cooldown_login(matricula, 1800)
            _marcar_erro(
                (err_msg or "Token rejeitado pela API do PAP.")
                + " Cooldown anti-bloqueio ativado (sem novo login automático)."
            )
            return

        _marcar_erro(err_msg or "Falha na busca do histórico PAP.")
    finally:
        try:
            automacao._fechar_sessao()
        except Exception:
            pass


def _job_cancelado(busca_id: int) -> bool:
    from crm_app.models import HistoricoPapBusca

    def _chk():
        st = HistoricoPapBusca.objects.filter(pk=busca_id).values_list("status", flat=True).first()
        return st == HistoricoPapBusca.STATUS_CANCELADO

    try:
        return bool(_run_django_sync(_chk))
    except Exception:
        return False


def _buscar_tipo(
    page, *, busca_id: int, tipo: str, data_ini: str, data_fim: str, pdv: str, token: str = ""
) -> dict:
    aliases = TIPO_API_ALIASES.get(tipo, (tipo,))
    last_err = ""
    for alias in aliases:
        if tipo == "PRE_VENDA":
            lista_status = ("PRE_VENDA", None)
        elif tipo in ("INTERESSE", "INTERESSE_SALVO"):
            lista_status = ("MINHAS_PENDENCIAS", None)
        else:
            lista_status = (STATUS_LISTA_PADRAO, None)

        for status in lista_status:
            url = montar_url_vendas(
                data_inicio=data_ini,
                data_fim=data_fim,
                pdv=pdv,
                tipo_api=alias,
                page=1,
                status=status,
            )
            resp = _fetch_json(page, url, token=token)
            if not isinstance(resp, dict):
                last_err = "resposta inválida"
                continue
            if not resp.get("ok"):
                last_err = f"HTTP {resp.get('status')} {resp.get('error') or ''}".strip()
                # 401/403: token/sessão inválidos — aborta todos os tipos
                if resp.get("status") in (401, 403):
                    return {
                        "encontrados": 0,
                        "novos": 0,
                        "ignorados": 0,
                        "novos_numeros": [],
                        "tipo_api": alias,
                        "erro": (
                            f"{last_err}. Sessão/token rejeitado pela API do PAP. "
                            "Não é bloqueio de login; verifique se a Ana abre o Histórico no PAP "
                            "e se a matrícula/senha estão corretas."
                        ),
                        "erro_fatal": True,
                    }
                continue
            lista, total = extrair_lista_api(resp.get("json"))
            if resp.get("status") == 200 and (lista is not None):
                # lista vazia com total 0 ainda é sucesso (período sem pedidos)
                return _paginar_tipo(
                    page,
                    busca_id=busca_id,
                    tipo=tipo,
                    tipo_api=alias,
                    data_ini=data_ini,
                    data_fim=data_fim,
                    pdv=pdv,
                    status=status,
                    primeira=lista or [],
                    total=total or 0,
                    
                )
        time.sleep(_intervalo())
    logger.warning("[HISTORICO PAP] Tipo %s não retornou dados (%s)", tipo, last_err)
    return {
        "encontrados": 0,
        "novos": 0,
        "ignorados": 0,
        "novos_numeros": [],
        "tipo_api": aliases[0],
        "erro": last_err,
    }


def _paginar_tipo(
    page,
    *,
    busca_id: int,
    tipo: str,
    tipo_api: str,
    data_ini: str,
    data_fim: str,
    pdv: str,
    status: Optional[str],
    primeira: list[dict],
    total: int,
    token: str = "",
) -> dict:
    encontrados = 0
    novos = 0
    ignorados = 0
    novos_numeros: list[str] = []
    paginas = max(1, (int(total or 0) + LIMIT_PAGINA - 1) // LIMIT_PAGINA) if total else 1
    paginas = min(paginas, 80)

    def _ingerir(lista: list[dict]):
        nonlocal encontrados, novos, ignorados
        for p in lista:
            ped = normalizar_pedido(p.get("numeroPedido") or p.get("pedido"))
            if not ped:
                continue
            encontrados += 1

            def _one():
                if _pedido_conhecido(ped):
                    return False
                return _salvar_novo(ped, tipo, pdv, p)

            if _run_django_sync(_one):
                novos += 1
                novos_numeros.append(ped)
            else:
                ignorados += 1

    _ingerir(primeira)
    for page_n in range(2, paginas + 1):
        if _job_cancelado(busca_id):
            break
        time.sleep(_intervalo())
        url = montar_url_vendas(
            data_inicio=data_ini,
            data_fim=data_fim,
            pdv=pdv,
            tipo_api=tipo_api,
            page=page_n,
            status=status,
        )
        resp = _fetch_json(page, url, token=token)
        if not isinstance(resp, dict) or not resp.get("ok"):
            logger.warning("[HISTORICO PAP] Falha página %s tipo %s: %s", page_n, tipo, resp)
            break
        lista, _ = extrair_lista_api(resp.get("json"))
        if not lista:
            break
        _ingerir(lista)
        _run_django_sync(
            lambda: _atualizar(
                busca_id,
                encontrados=encontrados,
                novos=novos,
                ignorados=ignorados,
                relatorio_json={"fase": f"{tipo} p.{page_n}/{paginas}"},
            )
        )
    return {
        "encontrados": encontrados,
        "novos": novos,
        "ignorados": ignorados,
        "novos_numeros": novos_numeros,
        "tipo_api": tipo_api,
    }
