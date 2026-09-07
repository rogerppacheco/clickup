"""Diagnóstico seguro do Histórico PAP — SEM login automático.

Objetivo: abrir o Chromium visível no Cursor, você controla o login manualmente
(ou cola um Bearer já capturado), e comparamos o Authorization real da SPA
contra a nossa montagem de headers — sem queimar tentativas na Nio.

Uso (recomendado — zero login automático):
  python scripts/diagnostico_pap_historico_seguro.py --token-manual "Bearer eyJ..."

  python scripts/diagnostico_pap_historico_seguro.py --aguardar-login-manual

  # Reusa cookies locais se existirem (NÃO faz login):
  python scripts/diagnostico_pap_historico_seguro.py --usar-sessao-local
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gestao_equipes.settings")
os.environ.setdefault("SECRET_KEY", os.environ.get("SECRET_KEY", "dev-secret-key-diagnostico"))
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PAP_HEADLESS"] = "false"

import django

django.setup()

from playwright.sync_api import sync_playwright

from crm_app.historico_pap import PAP_HISTORICO_URL, STATUS_LISTA_PADRAO, montar_url_vendas
from crm_app.historico_pap_service import (
    _fetch_json,
    _gerar_anti_replay_hash,
    _headers_auth,
    limpar_jwt,
    validar_e_decodificar_jwt,
)


def _url_teste(dias: int = 1) -> str:
    hoje = date.today()
    ini = hoje - timedelta(days=max(0, dias - 1))
    return montar_url_vendas(
        data_inicio=f"{ini.isoformat()}T00:00:00-03:00",
        data_fim=f"{hoje.isoformat()}T23:59:59-03:00",
        pdv="",
        tipo_api="VENDA",
        page=1,
        limit=15,
        status=STATUS_LISTA_PADRAO,
    )


def _analisar_token(rotulo: str, token: str) -> dict:
    limpo = limpar_jwt(token)
    ok, payload, clean_or_msg = validar_e_decodificar_jwt(limpo or token)
    parts = (limpo or "").split(".")
    sig = parts[2] if len(parts) == 3 else ""
    info = {
        "rotulo": rotulo,
        "ok_jwt": ok,
        "len": len(limpo or token or ""),
        "dots": (limpo or "").count("."),
        "sig_len": len(sig),
        "tem_hash_36": len(sig) >= 79,
        "uuid": (payload or {}).get("uuid") if ok else None,
        "origem": (payload or {}).get("origem") if ok else None,
        "inicio": (limpo or token or "")[:40],
        "fim": (limpo or token or "")[-40:],
        "msg": None if ok else clean_or_msg,
    }
    print(f"\n=== TOKEN [{rotulo}] ===")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return info


def _testar_variantes(page, token_spa: str, url: str) -> None:
    limpo = limpar_jwt(token_spa)
    variantes = [
        ("spa-exato-preservado", _headers_auth(limpo, regenerar_anti_replay=False)),
        ("anti-replay-nosso", _headers_auth(limpo, regenerar_anti_replay=True)),
    ]
    # JWT puro (sem hash) + nosso hash
    parts = limpo.split(".")
    if len(parts) == 3 and len(parts[2]) >= 79:
        puro = f"{parts[0]}.{parts[1]}.{parts[2][:-36]}"
        variantes.append(("jwt-puro+nosso-hash", _headers_auth(puro, regenerar_anti_replay=True)))
        variantes.append(
            (
                "jwt-puro-sem-hash",
                {
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://pap.niointernet.com.br",
                    "Referer": PAP_HISTORICO_URL,
                    "Authorization": f"Bearer {puro}",
                },
            )
        )

    print("\n=== COMPARAÇÃO DE HASH ===")
    nosso = _gerar_anti_replay_hash()
    spa_hash = parts[2][-36:] if len(parts) == 3 and len(parts[2]) >= 79 else ""
    print(f"hash SPA (últimos 36): {spa_hash}")
    print(f"hash nosso gerado agora: {nosso}")
    print(f"iguais? {spa_hash == nosso}")

    print(f"\n=== TESTES HTTP na URL ===\n{url}\n")
    for nome, headers in variantes:
        auth = headers.get("Authorization", "")
        print(f"\n--- Variante: {nome} | Authorization len={len(auth)} ---")
        # Via página (mesmo contexto)
        if page is not None:
            res = _fetch_json(page, url, token=limpo if "spa" in nome or "anti-replay" in nome else limpo)
            # Forçar headers específicos via evaluate
            try:
                res2 = page.evaluate(
                    """
                    async ({ url, authVal }) => {
                      const hdrs = { Accept: 'application/json, text/plain, */*' };
                      if (authVal) hdrs.Authorization = authVal;
                      const r = await fetch(url, { method: 'GET', credentials: 'include', headers: hdrs });
                      const text = await r.text();
                      return { status: r.status, ok: r.ok, preview: text.slice(0, 220) };
                    }
                    """,
                    {"url": url, "authVal": auth},
                )
                print(f"evaluate fetch: status={res2.get('status')} ok={res2.get('ok')} preview={res2.get('preview')}")
            except Exception as exc:
                print(f"evaluate fetch erro: {exc}")
                print(f"_fetch_json fallback: {res}")
        else:
            import requests

            r = requests.get(url, headers=headers, timeout=60)
            print(f"requests: status={r.status_code} preview={r.text[:220]}")


def main():
    parser = argparse.ArgumentParser(description="Diagnóstico seguro Histórico PAP (sem login automático)")
    parser.add_argument("--token-manual", default="", help="Cole o Authorization completo (com ou sem Bearer)")
    parser.add_argument("--aguardar-login-manual", action="store_true", help="Abre browser e espera VOCÊ logar")
    parser.add_argument("--usar-sessao-local", action="store_true", help="Reusa pap_sessions/*.json sem logar")
    parser.add_argument("--matricula", default="", help="Só para escolher arquivo de sessão local")
    parser.add_argument("--dias", type=int, default=1)
    args = parser.parse_args()

    url = _url_teste(args.dias)
    print("DIAGNÓSTICO SEGURO — nenhum login automático será disparado pelo script.")
    print(f"URL de teste: {url}")

    if args.token_manual.strip():
        tok = args.token_manual.strip()
        _analisar_token("manual", tok)
        _testar_variantes(None, tok, url)
        return

    sessions_dir = os.path.join(BASE_DIR, "pap_sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    session_file = None
    if args.matricula:
        candidato = os.path.join(sessions_dir, f"pap_session_{args.matricula.strip()}.json")
        if os.path.exists(candidato):
            session_file = candidato
    if not session_file:
        # pega a mais recente
        files = [
            os.path.join(sessions_dir, f)
            for f in os.listdir(sessions_dir)
            if f.startswith("pap_session_") and f.endswith(".json")
        ]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        session_file = files[0] if files else None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=50,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        opts = {
            "viewport": {"width": 1366, "height": 768},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if args.usar_sessao_local and session_file:
            print(f"[INFO] Reusando sessão local: {session_file}")
            opts["storage_state"] = session_file
        elif args.usar_sessao_local:
            print("[AVISO] Nenhuma sessão local encontrada; abrindo limpo.")

        context = browser.new_context(**opts)
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined })")

        captured = {"auth": "", "url": ""}

        def on_req(req):
            try:
                u = (req.url or "").lower()
                if "pap-api.niointernet.com.br" not in u:
                    return
                auth = req.headers.get("authorization") or req.headers.get("Authorization") or ""
                if auth and len(auth) > 20:
                    captured["auth"] = auth
                    captured["url"] = req.url
                    print(f"\n[CAPTURADO] {req.method} {req.url[:120]}")
                    print(f"[CAPTURADO] Authorization len={len(auth)} prefix={auth[:24]}...")
            except Exception:
                pass

        page.on("request", on_req)

        if args.aguardar_login_manual:
            print("\n>>> Abra/faça login MANUALMENTE na janela do Chromium.")
            print(">>> Depois navegue até Histórico de Pedidos (ou aguarde o script).")
            print(">>> O script NÃO digita matrícula/senha.")
            page.goto("https://pap.niointernet.com.br/", wait_until="domcontentloaded", timeout=60000)
            print("Aguardando até 10 minutos por um Authorization em pap-api...")
            fim = time.time() + 600
            while time.time() < fim and not captured["auth"]:
                time.sleep(1)
            if not captured["auth"]:
                print("[ERRO] Nenhum Authorization capturado. Encerre e tente com --token-manual.")
                browser.close()
                return
        else:
            print("\n>>> Indo ao Histórico (sem login automático). Se pedir login, faça manualmente.")
            page.goto(PAP_HISTORICO_URL, wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):
                if captured["auth"]:
                    break
                page.wait_for_timeout(1000)

        if not captured["auth"]:
            print("[ERRO] Sem Authorization da SPA. Use --aguardar-login-manual ou --token-manual.")
            print("Dica: no Chrome DevTools > Network > qualquer request pap-api > Request Headers > Authorization")
            browser.close()
            return

        _analisar_token("spa-capturado", captured["auth"])
        _testar_variantes(page, captured["auth"], url)

        # Salva sessão para reuso futuro (sem novo login)
        out = os.path.join(sessions_dir, "pap_session_diagnostico.json")
        try:
            context.storage_state(path=out)
            print(f"\n[OK] Sessão salva em {out}")
        except Exception as exc:
            print(f"[AVISO] Não salvou sessão: {exc}")

        print("\nNavegador permanece aberto 60s para inspeção...")
        page.wait_for_timeout(60000)
        browser.close()


if __name__ == "__main__":
    main()
