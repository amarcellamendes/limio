"""
Serviço de geração de boletos bancários.

Provedores suportados:
  - mock    : boleto fictício para testes/demo (padrão)
  - asaas   : Asaas API (https://asaas.com) — mais popular para PMEs
  - gerencianet: Efí Bank / Gerencianet API

Para ativar em produção:
  1. Configure boleto_provider = 'asaas' no cadastro do cliente
  2. Gere uma API key em app.asaas.com → Configurações → Integrações
  3. Salve a API key em boleto_api_key do cliente
"""
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx


def _mock_linha_digitavel() -> str:
    """Gera linha digitável fictícia no formato bancário brasileiro."""
    def bloco(n): return "".join(random.choices(string.digits, k=n))
    return f"{bloco(5)}.{bloco(5)} {bloco(5)}.{bloco(6)} {bloco(5)}.{bloco(6)} {random.randint(1,9)} {bloco(14)}"


def _mock_codigo_barras() -> str:
    return "".join(random.choices(string.digits, k=44))


async def emitir_boleto(
    provider: str,
    api_key: Optional[str],
    nota_data: dict,
    tomador: dict,
    dias_vencimento: int = 3,
) -> dict:
    """
    Emite boleto para a nota fiscal.

    Retorna dict com:
      linha_digitavel, codigo_barras, url, vencimento, status
    """
    vencimento = (datetime.now(timezone.utc) + timedelta(days=dias_vencimento)).date()

    if provider == "asaas":
        return await _emitir_asaas(api_key, nota_data, tomador, vencimento)
    if provider == "gerencianet":
        return await _emitir_gerencianet(api_key, nota_data, tomador, vencimento)

    # Default: mock
    return _emitir_mock(nota_data, vencimento)


def _emitir_mock(nota_data: dict, vencimento) -> dict:
    linha = _mock_linha_digitavel()
    return {
        "linha_digitavel": linha,
        "codigo_barras": _mock_codigo_barras(),
        "url": None,
        "vencimento": vencimento.isoformat(),
        "status": "pendente",
        "provider": "mock",
        "aviso": "Boleto simulado — configure um provedor real (Asaas) para emitir boletos registrados.",
    }


async def _emitir_asaas(api_key: str, nota_data: dict, tomador: dict, vencimento) -> dict:
    """
    Asaas API v3.
    Documentação: https://docs.asaas.com/reference/criar-nova-cobranca
    """
    base_url = "https://api.asaas.com/v3"
    headers = {
        "access_token": api_key,
        "Content-Type": "application/json",
    }

    cpf_cnpj = (tomador.get("cpf_cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
    nome = tomador.get("razao_social") or tomador.get("nome") or "Cliente"

    async with httpx.AsyncClient(timeout=30) as http:
        # 1. Buscar/criar customer pelo CPF/CNPJ
        r = await http.get(f"{base_url}/customers", headers=headers,
                           params={"cpfCnpj": cpf_cnpj})
        r.raise_for_status()
        clientes = r.json().get("data", [])

        if clientes:
            customer_id = clientes[0]["id"]
        else:
            rc = await http.post(f"{base_url}/customers", headers=headers,
                                 json={"name": nome, "cpfCnpj": cpf_cnpj,
                                       "email": tomador.get("email") or ""})
            rc.raise_for_status()
            customer_id = rc.json()["id"]

        # 2. Criar cobrança
        valor = float(nota_data.get("valor_liquido") or nota_data.get("valor_servico") or 0)
        numero = nota_data.get("numero") or nota_data.get("numero_rps") or ""
        rp = await http.post(f"{base_url}/payments", headers=headers, json={
            "customer": customer_id,
            "billingType": "BOLETO",
            "value": round(valor, 2),
            "dueDate": vencimento.isoformat(),
            "description": f"NFS-e nº {numero} — {nota_data.get('discriminacao','')[:100]}",
            "externalReference": str(nota_data.get("id") or ""),
        })
        rp.raise_for_status()
        pg = rp.json()

        return {
            "linha_digitavel": pg.get("nossoNumero") or pg.get("identificationField") or "",
            "codigo_barras": pg.get("barCode") or "",
            "url": pg.get("bankSlipUrl") or pg.get("invoiceUrl") or "",
            "vencimento": vencimento.isoformat(),
            "status": "pendente",
            "provider": "asaas",
            "asaas_id": pg.get("id"),
        }


async def _emitir_gerencianet(api_key: str, nota_data: dict, tomador: dict, vencimento) -> dict:
    """Efí Bank (Gerencianet) — esqueleto para integração futura."""
    raise NotImplementedError(
        "Integração com Gerencianet/Efí Bank ainda não implementada. "
        "Use o provedor 'asaas' ou 'mock'."
    )
