from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import requests
import os
from dotenv import load_dotenv
from datetime import datetime
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GLPI_URL_BASE = os.getenv("GLPI_URL_BASE", "https://glpi-hmg.petacorp.com.br")
GLPI_API_URL = os.getenv("GLPI_API_URL", f"{GLPI_URL_BASE}/apirest.php")
GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN")
GLPI_USER_TOKEN = os.getenv("GLPI_USER_TOKEN")

MAPA_GRUPOS = {
    98: "Raiz"
}

MAPA_TECNICOS = {
    "otoniel.dias@petacorp.com.br": "@otoniel"
}


class ItemPayload(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None


class WebhookPayload(BaseModel):
    id: Optional[str] = None
    ticket_id: Optional[str] = None
    tickets_id: Optional[str] = None
    event: Optional[str] = None
    item: Optional[ItemPayload] = None


def enviar_telegram(texto):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {
            "ok": False,
            "erro": "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID nao configurado"
        }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto
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


def traduzir_status(status):
    mapa = {
        1: "Novo",
        2: "Em atendimento (atribuido)",
        3: "Em planejamento",
        4: "Pendente",
        5: "Solucionado",
        6: "Fechado"
    }
    if isinstance(status, int):
        return mapa.get(status, str(status))
    return str(status) if status is not None else "Nao informado"


def buscar_ticket(ticket_id, session_token):
    response = get_glpi(f"Ticket/{ticket_id}", session_token)

    if not response.ok:
        raise Exception(f"Erro ao buscar Ticket/{ticket_id}: status={response.status_code} resposta={response.text}")

    try:
        return response.json()
    except Exception:
        raise Exception(f"Resposta invalida ao buscar Ticket/{ticket_id}: {response.text}")


def buscar_nome_usuario(users_id, session_token):
    if not users_id:
        return None

    response = get_glpi(f"User/{users_id}", session_token)

    if not response.ok:
        return None

    try:
        data = response.json()
        return data.get("name") or data.get("realname") or f"Usuario {users_id}"
    except Exception:
        return None


def buscar_nome_grupo(groups_id, session_token):
    if not groups_id:
        return None

    try:
        groups_id_int = int(groups_id)
    except Exception:
        groups_id_int = groups_id

    if groups_id_int in MAPA_GRUPOS:
        return MAPA_GRUPOS[groups_id_int]

    response = get_glpi(f"Group/{groups_id}", session_token)

    if not response.ok:
        return f"Grupo {groups_id}"

    try:
        data = response.json()
        return data.get("name") or f"Grupo {groups_id}"
    except Exception:
        return f"Grupo {groups_id}"


def buscar_grupos_tecnicos(ticket_id, session_token):
    tecnicos = []
    grupos = []

    resp_users = get_glpi(f"Ticket/{ticket_id}/Ticket_User", session_token)
    if resp_users.ok:
        try:
            for item in resp_users.json():
                if int(item.get("type", 0)) == 2:
                    nome = buscar_nome_usuario(item.get("users_id"), session_token)
                    if nome:
                        tecnicos.append(nome)
        except Exception as e:
            print("Erro Ticket_User:", e)
            print("Resposta Ticket_User:", resp_users.text)

    resp_groups = get_glpi(f"Ticket/{ticket_id}/Group_Ticket", session_token)
    if resp_groups.ok:
        try:
            for item in resp_groups.json():
                if int(item.get("type", 0)) == 2:
                    nome = buscar_nome_grupo(item.get("groups_id"), session_token)
                    if nome:
                        grupos.append(nome)
        except Exception as e:
            print("Erro Group_Ticket:", e)
            print("Resposta Group_Ticket:", resp_groups.text)

    return list(dict.fromkeys(grupos)), list(dict.fromkeys(tecnicos))


def formatar_tecnicos(tecnicos):
    saida = []
    for tecnico in tecnicos:
        saida.append(MAPA_TECNICOS.get(tecnico, tecnico))
    return ", ".join(saida) if saida else "Nao informado"


def montar_mensagem(ticket, grupos, tecnicos):
    ticket_id = ticket.get("id", "Nao informado")
    titulo = ticket.get("name") or "Sem titulo"
    status = traduzir_status(ticket.get("status"))
    prioridade = ticket.get("priority", "Nao informado")
    grupo_txt = ", ".join(grupos) if grupos else "Nao informado"
    tecnico_txt = formatar_tecnicos(tecnicos)
    link = f"{GLPI_URL_BASE}/front/ticket.form.php?id={ticket_id}"

    return (
        f"Novo evento de chamado\n\n"
        f"Grupo: {grupo_txt}\n"
        f"Tecnico: {tecnico_txt}\n"
        f"Id: {ticket_id}\n"
        f"Titulo: {titulo}\n"
        f"Status: {status}\n"
        f"Prioridade: {prioridade}\n"
        f"Link: {link}"
    )


@app.get("/")
def home():
    return {
        "status": "ok",
        "mensagem": "API GLPI Telegram funcionando"
    }


@app.post("/webhook")
def webhook(payload: WebhookPayload):
    try:
        ticket_id_raw = (
            payload.id
            or payload.ticket_id
            or payload.tickets_id
            or (payload.item.id if payload.item else None)
        )

        if not ticket_id_raw:
            return {
                "status": "erro",
                "recebido": False,
                "erro": "Informe id, ticket_id, tickets_id ou item.id",
                "payload_recebido": payload.model_dump()
            }

        ticket_id = int(ticket_id_raw)

        session_token = iniciar_sessao_glpi()

        try:
            ticket = buscar_ticket(ticket_id, session_token)
            grupos, tecnicos = buscar_grupos_tecnicos(ticket_id, session_token)
            texto = montar_mensagem(ticket, grupos, tecnicos)
            telegram = enviar_telegram(texto)

            return {
                "status": "ok",
                "recebido": True,
                "ticket_id": ticket_id,
                "payload_recebido": payload.model_dump(),
                "ticket": ticket,
                "grupos": grupos,
                "tecnicos": tecnicos,
                "mensagem_enviada": texto,
                "telegram": telegram,
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