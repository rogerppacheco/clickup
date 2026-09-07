"""Testes de validação de JWT, cache de token e circuit breaker anti-bloqueio no Histórico PAP."""
import base64
import json
import time
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase, TestCase

from crm_app.historico_pap_service import (
    validar_e_decodificar_jwt,
    salvar_token_cache,
    obter_token_cache,
    remover_token_cache,
    registrar_cooldown_login,
    verificar_cooldown_login,
    limpar_cooldown_login,
    obter_status_sessao_pap,
    _fetch_json,
    _headers_auth,
)


def _gerar_jwt_mock(exp_em_segundos: float = 3600, payload_extra: dict = None) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": "ana_diretoria", "exp": time.time() + exp_em_segundos}
    if payload_extra:
        payload.update(payload_extra)
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJ{h_b64[3:]}.{p_b64}.mock_signature_part"


class HistoricoPapTokenValidationTest(SimpleTestCase):
    def test_token_valido(self):
        tok = _gerar_jwt_mock(3600)
        ok, payload, clean = validar_e_decodificar_jwt(tok)
        self.assertTrue(ok)
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("sub"), "ana_diretoria")
        self.assertEqual(clean, tok)

    def test_token_com_prefixo_bearer(self):
        tok = _gerar_jwt_mock(3600)
        ok, payload, clean = validar_e_decodificar_jwt(f"Bearer {tok}")
        self.assertTrue(ok)
        self.assertEqual(clean, tok)

    def test_token_expirado(self):
        tok = _gerar_jwt_mock(-100)  # Expirou há 100 segundos
        ok, payload, msg = validar_e_decodificar_jwt(tok)
        self.assertFalse(ok)
        self.assertIn("Token expirado", msg)

    def test_token_invalido_formato(self):
        ok, _, msg = validar_e_decodificar_jwt("token_simples_invalido")
        self.assertFalse(ok)
        self.assertIn("Token não possui estrutura de JWT", msg)

    def test_headers_auth(self):
        parts = _gerar_jwt_mock(3600).split(".")
        # Token SPA já com hash (sig=79) deve ser preservado
        base = f"{parts[0]}.{parts[1]}.{'c' * 43}"
        tok_spa = base + ("H" * 36)
        h = _headers_auth(tok_spa, regenerar_anti_replay=False)
        self.assertEqual(h["Authorization"], f"Bearer {tok_spa}")
        self.assertNotIn("Origem", h)
        self.assertIn("pap.niointernet.com.br", h["Origin"])
        self.assertIn("administrativo/historico", h["Referer"])

    def test_headers_auth_com_bearer(self):
        parts = _gerar_jwt_mock(3600).split(".")
        base = f"{parts[0]}.{parts[1]}.{'d' * 43}"
        tok_spa = base + ("H" * 36)
        h = _headers_auth(f"Bearer {tok_spa}", regenerar_anti_replay=False)
        self.assertEqual(h["Authorization"], f"Bearer {tok_spa}")

    def test_headers_auth_regenera_quando_pedido(self):
        parts = _gerar_jwt_mock(3600).split(".")
        base = f"{parts[0]}.{parts[1]}.{'e' * 43}"
        tok_spa = base + ("F" * 36)
        h = _headers_auth(tok_spa, regenerar_anti_replay=True)
        raw = h["Authorization"][len("Bearer "):]
        self.assertEqual(len(raw), len(base) + 36)
        self.assertTrue(raw.startswith(base))
        self.assertFalse(raw.endswith("F" * 36))

    def test_headers_auth_gera_hash_para_jwt_puro(self):
        parts = _gerar_jwt_mock(3600).split(".")
        tok43 = f"{parts[0]}.{parts[1]}.{'g' * 43}"
        h = _headers_auth(tok43, regenerar_anti_replay=False)
        raw = h["Authorization"][len("Bearer "):]
        self.assertEqual(len(raw), len(tok43) + 36)
        self.assertTrue(raw.startswith(tok43))

    def test_anti_replay_hash_tem_36_chars(self):
        from crm_app.historico_pap_service import _gerar_anti_replay_hash

        h = _gerar_anti_replay_hash()
        self.assertEqual(len(h), 36)

    def test_token_com_payload_nio_uuid(self):
        # Tokens emitidos pelo PAP Nio possuem "uuid" e "origem: bo", não "sub"
        tok = _gerar_jwt_mock(3600, payload_extra={"uuid": "TT713110-1234", "origem": "bo"})
        ok, payload, clean = validar_e_decodificar_jwt(tok)
        self.assertTrue(ok)
        self.assertEqual(payload.get("uuid"), "TT713110-1234")
        self.assertEqual(payload.get("origem"), "bo")

    def test_token_assinatura_com_caracteres_base64(self):
        # Assinaturas com +, / ou padding = não devem ser truncadas
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"uuid":"TT713110"}').decode().rstrip("=")
        sig = "sig123+abc/xyz=="
        tok = f"eyJ{header[3:]}.{payload}.{sig}"
        ok, payload_dict, clean = validar_e_decodificar_jwt(tok)
        self.assertTrue(ok)
        self.assertEqual(clean, tok)


class HistoricoPapCacheECooldownTest(SimpleTestCase):
    def setUp(self):
        limpar_cooldown_login("12345")
        remover_token_cache("12345")

    def tearDown(self):
        limpar_cooldown_login("12345")
        remover_token_cache("12345")

    def test_cache_salvar_e_obter(self):
        tok = _gerar_jwt_mock(1800)
        salvar_token_cache("12345", tok)
        cached, payload = obter_token_cache("12345")
        self.assertEqual(cached, tok)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "ana_diretoria")

    def test_cooldown_registro_e_verificacao(self):
        em_cd, _ = verificar_cooldown_login("12345")
        self.assertFalse(em_cd)

        registrar_cooldown_login("12345", 300)
        em_cd2, seg2 = verificar_cooldown_login("12345")
        self.assertTrue(em_cd2)
        self.assertGreater(seg2, 250)

        limpar_cooldown_login("12345")
        em_cd3, _ = verificar_cooldown_login("12345")
        self.assertFalse(em_cd3)

    def test_status_sessao(self):
        tok = _gerar_jwt_mock(1800)
        salvar_token_cache("12345", tok)
        status = obter_status_sessao_pap("12345")
        self.assertTrue(status["tem_token_valido"])
        self.assertGreaterEqual(status["expira_em_minutos"], 28)
        self.assertFalse(status["cooldown_ativo"])


class HistoricoPapFetchDirectHttpTest(SimpleTestCase):
    @patch("crm_app.historico_pap_service.requests.get")
    def test_fetch_direto_sem_playwright(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"total": 1, "data": [{"numeroPedido": "202609051234567890"}]}'
        mock_resp.json.return_value = {"total": 1, "data": [{"numeroPedido": "202609051234567890"}]}
        mock_get.return_value = mock_resp

        tok = _gerar_jwt_mock(3600)
        resp = _fetch_json(page=None, url="https://pap-api.niointernet.com.br/api/portal/vendas", token=tok)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["status"], 200)
        self.assertEqual(resp["json"]["total"], 1)

        # Verificar headers: token SPA com hash deve ser preservado
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        auth = kwargs["headers"]["Authorization"]
        self.assertTrue(auth.startswith("Bearer "))
        self.assertNotIn("Origem", kwargs["headers"])
