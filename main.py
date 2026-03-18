# -*- coding: utf-8 -*-

from fastapi import FastAPI, Request
import requests
import os
import json
import urllib3
from dotenv import load_dotenv
from datetime import datetime
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GLPI_URL_BASE = os.getenv("GLPI_URL_BASE", "https://glpi-hmg.petacorp.com.br")
GLPI_API_URL = os.getenv("GLPI_API_URL", f"{GLPI_URL_BASE}/apirest.php")
GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN")
GLPI_USER_TOKEN = os.getenv("GLPI_USER_TOKEN")

STATE_FILE = Path("estado_main.json")

MAPA_TECNICOS = {
    # Exemplo:
    # "otoniel.dias@petacorp.com.br": "@otoniel",
    # "Otoniel Dias": "@otoniel",
}


def carregar_estado():
    if not STATE_FILE.exists():
        return {"tickets": {}}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("tickets", {})
                return data
    except Exception as e:
        print("Erro ao carregar estado_main.json:", e)

    return {"tickets": {}}


def salvar_estado(estado):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Erro ao salvar estado_main.json:", e)


ESTADO = carregar_estado()


def enviar_telegram(texto):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {
            "ok": False,
            "erro": "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID nao configurado"
        }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(url, json=payload, timeout=15, verify=False)
        return {
            "ok": response.ok,
            "status_code": response.status_code,
            "resposta": response.text
        }
    except Exception as e:
        return {
            "ok": False,
            "erro": str(e)
        }


def iniciar_sessao_glpi():
    if not GLPI_API_URL or not GLPI_APP_TOKEN or not GLPI_USER_TOKEN:
        raise Exception("GLPI_API_URL, GLPI_APP_TOKEN ou GLPI_USER_TOKEN nao configurado")

    url = f"{GLPI_API_URL}/initSession"
    headers = {
        "App-Token": GLPI_APP_TOKEN,
        "Authorization": f"user_token {GLPI_USER_TOKEN}"
    }

    response = requests.get(url, headers=headers, timeout=15, verify=False)

    try:
        data = response.json()
    except Exception:
        raise Exception(f"Resposta invalida no initSession: {response.text}")

    if response.ok and "session_token" in data:
        return data["session_token"]

    raise Exception(f"Erro initSession: status={response.status_code} resposta={data}")


def encerrar_sessao_glpi(session_token):
    try:
        url = f"{GLPI_API_URL}/killSession"
        headers = {
            "App-Token": GLPI_APP_TOKEN,
            "Session-Token": session_token
        }
        requests.get(url, headers=headers, timeout=10, verify=False)
    except Exception:
        pass


def get_glpi(endpoint, session_token, params=None):
    url = f"{GLPI_API_URL}/{endpoint.lstrip('/')}"
    headers = {
        "App-Token": GLPI_APP_TOKEN,
        "Session-Token": session_token
    }
    return requests.get(url, headers=headers, params=params, timeout=20, verify=False)


def normalizar_lista_api(data):
    if isinstance(data, dict) and "data" in data:
        data = data["data"]

    if isinstance(data, list):
        return data

    return []


def buscar_ticket(ticket_id, session_token):
    response = get_glpi(f"Ticket/{ticket_id}", session_token)

    if not response.ok:
        raise Exception(
            f"Erro ao buscar Ticket/{ticket_id}: "
            f"status={response.status_code} resposta={response.text}"
        )

    try:
        return response.json()
    except Exception:
        raise Exception(f"Resposta invalida ao buscar Ticket/{ticket_id}: {response.text}")


def buscar_nome_grupo(groups_id, session_token):
    response = get_glpi(f"Group/{groups_id}", session_token)

    if not response.ok:
        return f"Grupo {groups_id}"

    try:
        data = response.json()
        return data.get("name") or f"Grupo {groups_id}"
    except Exception:
        return f"Grupo {groups_id}"


def buscar_usuario(users_id, session_token):
    response = get_glpi(f"User/{users_id}", session_token)

    if not response.ok:
        return {
            "id": users_id,
            "nome": f"Usuario {users_id}",
            "email": None
        }

    try:
        data = response.json()
    except Exception:
        return {
            "id": users_id,
            "nome": f"Usuario {users_id}",
            "email": None
        }

    nome = data.get("realname") or data.get("name") or f"Usuario {users_id}"
    email = data.get("email")

    return {
        "id": users_id,
        "nome": nome,
        "email": email
    }


def formatar_tecnico(tecnico):
    nome = tecnico.get("nome") or "Nao informado"
    email = tecnico.get("email")

    if email and email in MAPA_TECNICOS:
        return MAPA_TECNICOS[email]

    if nome in MAPA_TECNICOS:
        return MAPA_TECNICOS[nome]

    return nome


def buscar_grupos_ticket(ticket_id, session_token):
    grupos = []

    response = get_glpi(f"Ticket/{ticket_id}/Group_Ticket", session_token)
    if not response.ok:
        return grupos

    try:
        itens = normalizar_lista_api(response.json())
    except Exception:
        return grupos

    for item in itens:
        try:
            if int(item.get("type", 0)) != 2:
                continue

            groups_id = int(item.get("groups_id"))
            grupos.append({
                "id": groups_id,
                "nome": buscar_nome_grupo(groups_id, session_token)
            })
        except Exception:
            continue

    unicos = {}
    for grupo in grupos:
        unicos[grupo["id"]] = grupo

    return list(unicos.values())


def buscar_tecnicos_ticket(ticket_id, session_token):
    tecnicos = []

    response = get_glpi(f"Ticket/{ticket_id}/Ticket_User", session_token)
    if not response.ok:
        return tecnicos

    try:
        itens = normalizar_lista_api(response.json())
    except Exception:
        return tecnicos

    for item in itens:
        try:
            if int(item.get("type", 0)) != 2:
                continue

            users_id = int(item.get("users_id"))
            usuario = buscar_usuario(users_id, session_token)
            tecnicos.append(usuario)
        except Exception:
            continue

    unicos = {}
    for tecnico in tecnicos:
        unicos[tecnico["id"]] = tecnico

    return list(unicos.values())


def obter_valor_caminho(dados, caminho):
    atual = dados
    for chave in caminho:
        if not isinstance(atual, dict):
            return None
        atual = atual.get(chave)
        if atual is None:
            return None
    return atual


def extrair_ticket_id(payload):
    if not isinstance(payload, dict):
        return None

    caminhos = [
        ("ticket_id",),
        ("tickets_id",),
        ("items_id",),
        ("ticket", "id"),
        ("item", "ticket_id"),
        ("data", "ticket_id"),
        ("data", "tickets_id"),
        ("data", "items_id"),
        ("input", "ticket_id"),
        ("input", "tickets_id"),
        ("input", "items_id"),
        ("item", "id"),
        ("data", "id"),
        ("input", "id"),
        ("id",),
    ]

    for caminho in caminhos:
        valor = obter_valor_caminho(payload, caminho)
        if valor is None:
            continue

        try:
            return int(valor)
        except Exception:
            continue

    return None


async def ler_payload_request(request: Request):
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception:
            pass

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            return dict(form)
        except Exception:
            pass

    try:
        body = await request.body()
        if body:
            texto = body.decode("utf-8", errors="ignore")
            try:
                return json.loads(texto)
            except Exception:
                return {"raw_body": texto}
    except Exception:
        pass

    return {}


def montar_mensagem_grupo(ticket, grupo, tecnicos):
    ticket_id = ticket.get("id")
    titulo = ticket.get("name") or "Sem titulo"
    link = f"{GLPI_URL_BASE}/front/ticket.form.php?id={ticket_id}"

    tecnico_txt = ", ".join([formatar_tecnico(t) for t in tecnicos]) if tecnicos else "Nao informado"

    return (
        f"Grupo atribuido ao chamado\n\n"
        f"Grupo: {grupo['nome']}\n"
        f"Tecnico: {tecnico_txt}\n"
        f"Id: {ticket_id}\n"
        f"Titulo: {titulo}\n"
        f"Link: {link}"
    )


def montar_mensagem_tecnico(ticket, grupos, tecnico):
    ticket_id = ticket.get("id")
    titulo = ticket.get("name") or "Sem titulo"
    link = f"{GLPI_URL_BASE}/front/ticket.form.php?id={ticket_id}"
    grupo_txt = ", ".join([g["nome"] for g in grupos]) if grupos else "Nao informado"

    return (
        f"Tecnico atribuido ao chamado\n\n"
        f"Grupo: {grupo_txt}\n"
        f"Tecnico: {formatar_tecnico(tecnico)}\n"
        f"Id: {ticket_id}\n"
        f"Titulo: {titulo}\n"
        f"Link: {link}"
    )


@app.get("/")
def home():
    return {
        "status": "ok",
        "mensagem": "API GLPI Telegram funcionando"
    }


@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await ler_payload_request(request)
        ticket_id = extrair_ticket_id(payload)

        if not ticket_id:
            return {
                "status": "erro",
                "recebido": False,
                "erro": "Nao foi possivel identificar o ticket_id no payload",
                "payload_recebido": payload,
                "data_recebimento": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            }

        session_token = iniciar_sessao_glpi()

        try:
            ticket = buscar_ticket(ticket_id, session_token)
            grupos_ticket = buscar_grupos_ticket(ticket_id, session_token)
            tecnicos_ticket = buscar_tecnicos_ticket(ticket_id, session_token)

            ticket_key = str(ticket_id)
            estado_ticket = ESTADO["tickets"].get(ticket_key, {
                "grupos": [],
                "tecnicos": []
            })

            grupos_anteriores = set(int(x) for x in estado_ticket.get("grupos", []))
            tecnicos_anteriores = set(int(x) for x in estado_ticket.get("tecnicos", []))

            grupos_atuais = set(g["id"] for g in grupos_ticket)
            tecnicos_atuais = set(t["id"] for t in tecnicos_ticket)

            novos_grupos = [g for g in grupos_ticket if g["id"] not in grupos_anteriores]
            novos_tecnicos = [t for t in tecnicos_ticket if t["id"] not in tecnicos_anteriores]

            alertas_enviados = []

            for grupo in novos_grupos:
                texto = montar_mensagem_grupo(ticket, grupo, tecnicos_ticket)
                telegram = enviar_telegram(texto)

                alertas_enviados.append({
                    "tipo": "atribuicao_grupo",
                    "grupo_id": grupo["id"],
                    "grupo_nome": grupo["nome"],
                    "mensagem": texto,
                    "telegram": telegram
                })

            for tecnico in novos_tecnicos:
                texto = montar_mensagem_tecnico(ticket, grupos_ticket, tecnico)
                telegram = enviar_telegram(texto)

                alertas_enviados.append({
                    "tipo": "atribuicao_tecnico",
                    "tecnico_id": tecnico["id"],
                    "tecnico_nome": tecnico["nome"],
                    "mensagem": texto,
                    "telegram": telegram
                })

            ESTADO["tickets"][ticket_key] = {
                "grupos": sorted(list(grupos_atuais)),
                "tecnicos": sorted(list(tecnicos_atuais))
            }
            salvar_estado(ESTADO)

            if alertas_enviados:
                return {
                    "status": "ok",
                    "recebido": True,
                    "ticket_id": ticket_id,
                    "tipo_alerta": "atribuicao",
                    "payload_recebido": payload,
                    "alertas_enviados": alertas_enviados,
                    "data_recebimento": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                }

            return {
                "status": "ok",
                "recebido": True,
                "ticket_id": ticket_id,
                "tipo_alerta": "sem_alerta",
                "motivo": "Sem nova atribuicao de grupo ou tecnico",
                "payload_recebido": payload,
                "data_recebimento": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            }

        finally:
            encerrar_sessao_glpi(session_token)

    except Exception as e:
        return {
            "status": "erro",
            "recebido": False,
            "erro": str(e),
            "data_recebimento": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }