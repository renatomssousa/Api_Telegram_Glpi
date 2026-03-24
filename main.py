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


def carregar_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def salvar_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalizar_lista_api(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("items"), list):
            return data["items"]
        if isinstance(data.get("results"), list):
            return data["results"]

    return []


ESTADO = carregar_json(STATE_FILE, {"tickets": {}})


def enviar_telegram(texto):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "erro": "Telegram nao configurado"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=20, verify=False)
        return {
            "ok": r.ok,
            "status_code": r.status_code,
            "resposta": r.text
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def iniciar_sessao():
    r = requests.get(
        f"{GLPI_API_URL}/initSession",
        headers={
            "App-Token": GLPI_APP_TOKEN,
            "Authorization": f"user_token {GLPI_USER_TOKEN}"
        },
        timeout=20,
        verify=False
    )

    try:
        data = r.json()
    except Exception:
        raise Exception(f"Erro initSession: resposta invalida - {r.text}")

    if not r.ok or "session_token" not in data:
        raise Exception(f"Erro initSession: {data}")

    return data["session_token"]


def encerrar_sessao(session_token):
    try:
        requests.get(
            f"{GLPI_API_URL}/killSession",
            headers={
                "App-Token": GLPI_APP_TOKEN,
                "Session-Token": session_token
            },
            timeout=10,
            verify=False
        )
    except Exception:
        pass


def get_glpi(url, session_token, params=None):
    r = requests.get(
        f"{GLPI_API_URL}/{url}",
        headers={
            "App-Token": GLPI_APP_TOKEN,
            "Session-Token": session_token
        },
        params=params,
        timeout=20,
        verify=False
    )
    return r


def get_glpi_json(url, session_token, params=None):
    r = get_glpi(url, session_token, params=params)

    try:
        data = r.json()
    except Exception:
        raise Exception(f"Resposta invalida em {url}: {r.text}")

    if not r.ok:
        raise Exception(f"Erro GLPI em {url}: {data}")

    return data


def buscar_ticket(ticket_id, session_token):
    data = get_glpi_json(f"Ticket/{ticket_id}", session_token)
    if not isinstance(data, dict):
        raise Exception(f"Ticket/{ticket_id} retornou formato invalido")
    return data


def buscar_nome_grupo(groups_id, session_token):
    try:
        data = get_glpi_json(f"Group/{groups_id}", session_token)
        if isinstance(data, dict):
            return data.get("name") or f"Grupo {groups_id}"
    except Exception:
        pass
    return f"Grupo {groups_id}"


def extrair_email_do_usuario(data):
    if not isinstance(data, dict):
        return None

    email = (data.get("email") or "").strip()
    if email and "@" in email:
        return email

    # no seu GLPI o campo "name" pode vir como e-mail/login
    login = (data.get("name") or "").strip()
    if login and "@" in login:
        return login

    # fallback em outros campos possíveis
    for chave in ["user_email", "default_email", "personal_email"]:
        valor = (data.get(chave) or "").strip()
        if valor and "@" in valor:
            return valor

    return None


def buscar_usuario(users_id, session_token):
    try:
        data = get_glpi_json(f"User/{users_id}", session_token)
    except Exception:
        return {
            "id": users_id,
            "nome": f"Usuario {users_id}",
            "email": None
        }

    firstname = (data.get("firstname") or "").strip() if isinstance(data, dict) else ""
    realname = (data.get("realname") or "").strip() if isinstance(data, dict) else ""
    login = (data.get("name") or "").strip() if isinstance(data, dict) else ""

    nome = " ".join([x for x in [firstname, realname] if x]).strip()
    if not nome:
        nome = login or f"Usuario {users_id}"

    email = extrair_email_do_usuario(data)

    return {
        "id": users_id,
        "nome": nome,
        "email": email
    }


def formatar_tecnico(tecnico):
    email = (tecnico.get("email") or "").strip()
    if email:
        return email

    nome = (tecnico.get("nome") or "").strip()
    if nome:
        return nome

    return "Nao informado"


def buscar_grupos_ticket(ticket_id, session_token):
    grupos = []

    try:
        data = get_glpi_json(f"Ticket/{ticket_id}/Group_Ticket", session_token)
    except Exception:
        return grupos

    itens = normalizar_lista_api(data)

    for item in itens:
        if not isinstance(item, dict):
            continue

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

    try:
        data = get_glpi_json(f"Ticket/{ticket_id}/Ticket_User", session_token)
    except Exception:
        return tecnicos

    itens = normalizar_lista_api(data)

    for item in itens:
        if not isinstance(item, dict):
            continue

        try:
            if int(item.get("type", 0)) != 2:
                continue

            users_id = int(item.get("users_id"))
            tecnicos.append(buscar_usuario(users_id, session_token))
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


def obter_ids_grupos(grupos):
    return sorted([int(g["id"]) for g in grupos if g.get("id") is not None])


def obter_ids_tecnicos(tecnicos):
    return sorted([int(t["id"]) for t in tecnicos if t.get("id") is not None])


def obter_novos_ids(lista_atual, lista_anterior):
    return sorted(list(set(lista_atual) - set(lista_anterior)))


def filtrar_grupos_por_ids(grupos, ids):
    ids_set = set(ids)
    return [g for g in grupos if int(g["id"]) in ids_set]


def filtrar_tecnicos_por_ids(tecnicos, ids):
    ids_set = set(ids)
    return [t for t in tecnicos if int(t["id"]) in ids_set]


def montar_texto_notificacao(ticket_id, ticket, grupos_novos, tecnicos_novos):
    titulo = (ticket.get("name") or "Sem titulo").strip()
    link = f"{GLPI_URL_BASE}/front/ticket.form.php?id={ticket_id}"

    grupos_txt = ", ".join([g["nome"] for g in grupos_novos]) if grupos_novos else "Nao informado"
    tecnicos_txt = ", ".join([formatar_tecnico(t) for t in tecnicos_novos]) if tecnicos_novos else "Nao informado"

    if grupos_novos and tecnicos_novos:
        evento = "Grupo e tecnico atribuidos ao chamado"
    elif grupos_novos:
        evento = "Grupo atribuido ao chamado"
    elif tecnicos_novos:
        evento = "Tecnico atribuido ao chamado"
    else:
        evento = "Atualizacao de atribuicao"

    texto = (
        f"{evento}\n\n"
        f"Grupo: {grupos_txt}\n"
        f"Tecnico: {tecnicos_txt}\n"
        f"Id: {ticket_id}\n"
        f"Titulo: {titulo}\n"
        f"Link: {link}"
    )

    return texto


@app.get("/")
def home():
    return {"status": "ok", "mensagem": "API GLPI Telegram funcionando"}


@app.get("/debug/estado")
def debug_estado():
    return ESTADO


@app.post("/webhook")
async def webhook(request: Request):
    session_token = None

    try:
        payload = await ler_payload_request(request)
        ticket_id = extrair_ticket_id(payload)

        if not ticket_id:
            return {
                "status": "ignorado",
                "motivo": "Nao foi possivel identificar o ticket_id",
                "payload_recebido": payload
            }

        session_token = iniciar_sessao()

        ticket = buscar_ticket(ticket_id, session_token)
        grupos_ticket = buscar_grupos_ticket(ticket_id, session_token)
        tecnicos_ticket = buscar_tecnicos_ticket(ticket_id, session_token)

        grupos_ids_atuais = obter_ids_grupos(grupos_ticket)
        tecnicos_ids_atuais = obter_ids_tecnicos(tecnicos_ticket)

        ticket_key = str(ticket_id)
        estado_anterior = ESTADO["tickets"].get(ticket_key, {
            "grupos": [],
            "tecnicos": []
        })

        grupos_ids_anteriores = sorted([int(x) for x in estado_anterior.get("grupos", [])])
        tecnicos_ids_anteriores = sorted([int(x) for x in estado_anterior.get("tecnicos", [])])

        novos_grupos_ids = obter_novos_ids(grupos_ids_atuais, grupos_ids_anteriores)
        novos_tecnicos_ids = obter_novos_ids(tecnicos_ids_atuais, tecnicos_ids_anteriores)

        grupos_novos = filtrar_grupos_por_ids(grupos_ticket, novos_grupos_ids)
        tecnicos_novos = filtrar_tecnicos_por_ids(tecnicos_ticket, novos_tecnicos_ids)

        ESTADO["tickets"][ticket_key] = {
            "grupos": grupos_ids_atuais,
            "tecnicos": tecnicos_ids_atuais,
            "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }
        salvar_json(STATE_FILE, ESTADO)

        if not grupos_novos and not tecnicos_novos:
            return {
                "status": "ignorado",
                "motivo": "Sem nova atribuicao de grupo ou tecnico",
                "ticket_id": ticket_id
            }

        texto = montar_texto_notificacao(ticket_id, ticket, grupos_novos, tecnicos_novos)
        telegram = enviar_telegram(texto)

        if not telegram.get("ok"):
            return {
                "status": "erro",
                "erro": f"Falha ao enviar Telegram: {telegram}",
                "ticket_id": ticket_id
            }

        return {
            "status": "ok",
            "ticket_id": ticket_id,
            "novos_grupos": grupos_novos,
            "novos_tecnicos": tecnicos_novos
        }

    except Exception as e:
        return {
            "status": "erro",
            "erro": str(e),
            "data_recebimento": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        }

    finally:
        if session_token:
            encerrar_sessao(session_token)