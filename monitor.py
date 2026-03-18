# -*- coding: utf-8 -*-

import time
import json
import os
import re
from pathlib import Path

import pymysql
import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

CONFIG_FILE = "monitor_config.json"
STATE_FILE = "estado_monitor.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GLPI_URL_BASE = os.getenv("GLPI_URL_BASE", "https://glpi-hmg.petacorp.com.br")


def carregar_config():
    if not Path(CONFIG_FILE).exists():
        raise Exception(f"Arquivo {CONFIG_FILE} nao encontrado")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


config = carregar_config()

DB_HOST = config["db"]["host"]
DB_USER = config["db"]["user"]
DB_PASS = config["db"]["pass"]
DB_NAME = config["db"]["name"]

INTERVALO = int(config.get("intervalo_segundos", 300))
SLA_PERCENT_ALERTA = float(config.get("sla_percent_alerta", 20))


def carregar_estado():
    if not os.path.exists(STATE_FILE):
        return {
            "alertados_sla": [],
            "last_followup_id": 0
        }

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        data.setdefault("alertados_sla", [])
        data.setdefault("last_followup_id", 0)
        return data


def salvar_estado(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


state = carregar_estado()


def enviar_telegram(texto):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID nao configurado")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(url, json=payload, timeout=15, verify=False)
        print("Telegram:", response.status_code, response.text)
    except Exception as e:
        print("Falha ao enviar Telegram:", e)


def conectar_db():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4"
    )


def limpar_html(texto):
    texto = texto or ""
    texto = re.sub(r"<br\s*/?>", "\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"</p>", "\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = re.sub(r"&nbsp;", " ", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


QUERY_SLA_PERCENT = """
SELECT
  gt.id,
  gt.name,
  GROUP_CONCAT(DISTINCT COALESCE(u.realname, u.name) ORDER BY COALESCE(u.realname, u.name) SEPARATOR ', ') AS tecnicos,
  GROUP_CONCAT(DISTINCT g.name ORDER BY g.name SEPARATOR ', ') AS grupos_nomes,
  gt.date AS data_abertura,
  gt.internal_time_to_resolve AS data_sla,
  COALESCE(gt.sla_waiting_duration, 0) AS sla_waiting_duration,
  (
    TIMESTAMPDIFF(SECOND, gt.date, gt.internal_time_to_resolve) + COALESCE(gt.sla_waiting_duration, 0)
  ) AS total_segundos,
  (
    TIMESTAMPDIFF(SECOND, NOW(), gt.internal_time_to_resolve) + COALESCE(gt.sla_waiting_duration, 0)
  ) AS restante_segundos
FROM glpi_tickets gt
LEFT JOIN glpi_groups_tickets ggt
  ON ggt.tickets_id = gt.id AND ggt.type = 2
LEFT JOIN glpi_groups g
  ON g.id = ggt.groups_id
LEFT JOIN glpi_tickets_users gtu
  ON gtu.tickets_id = gt.id AND gtu.type = 2
LEFT JOIN glpi_users u
  ON u.id = gtu.users_id
WHERE gt.is_deleted = 0
  AND gt.status NOT IN (5, 6)
  AND gt.internal_time_to_resolve IS NOT NULL
GROUP BY
  gt.id,
  gt.name,
  gt.date,
  gt.internal_time_to_resolve,
  gt.sla_waiting_duration
ORDER BY gt.id ASC;
"""

QUERY_FOLLOWUP_REQUERENTE = """
SELECT
  f.id AS followup_id,
  f.items_id AS ticket_id,
  f.users_id AS followup_user_id,
  t.users_id_recipient,
  f.date AS followup_date,
  f.content AS followup_content,
  t.name AS ticket_title,
  GROUP_CONCAT(DISTINCT g.name ORDER BY g.name SEPARATOR ', ') AS grupos_nomes
FROM glpi_itilfollowups f
JOIN glpi_tickets t
  ON t.id = f.items_id
LEFT JOIN glpi_groups_tickets ggt
  ON ggt.tickets_id = t.id AND ggt.type = 2
LEFT JOIN glpi_groups g
  ON g.id = ggt.groups_id
WHERE t.is_deleted = 0
  AND t.status = 4
  AND f.users_id = t.users_id_recipient
  AND f.id > %s
GROUP BY
  f.id,
  f.items_id,
  f.users_id,
  t.users_id_recipient,
  f.date,
  f.content,
  t.name
ORDER BY f.id ASC;
"""


def processar_sla(cursor):
    cursor.execute(QUERY_SLA_PERCENT)
    rows = cursor.fetchall()

    ativos_abaixo_limite = set()

    for row in rows:
        ticket_id = int(row["id"])

        total = row.get("total_segundos")
        restante = row.get("restante_segundos")

        if total is None or restante is None:
            continue

        if total <= 0:
            continue

        percent_restante = (restante / total) * 100.0

        if percent_restante <= SLA_PERCENT_ALERTA:
            ativos_abaixo_limite.add(ticket_id)

            if ticket_id not in state["alertados_sla"]:
                grupos = row.get("grupos_nomes") or "Nao informado"
                tecnicos = row.get("tecnicos") or "Nao informado"
                link = f"{GLPI_URL_BASE}/front/ticket.form.php?id={ticket_id}"

                texto = (
                    f"SLA perto de vencer\n\n"
                    f"Grupos: {grupos}\n"
                    f"Tecnicos: {tecnicos}\n"
                    f"Id: {ticket_id}\n"
                    f"Titulo: {row.get('name')}\n"
                    f"Percentual restante: {percent_restante:.2f}%\n"
                    f"Data SLA: {row.get('data_sla')}\n"
                    f"Link: {link}"
                )

                enviar_telegram(texto)

    state["alertados_sla"] = sorted(list(ativos_abaixo_limite))


def processar_followup(cursor):
    last_id = int(state["last_followup_id"] or 0)

    cursor.execute(QUERY_FOLLOWUP_REQUERENTE, (last_id,))
    rows = cursor.fetchall()

    for row in rows:
        followup_id = int(row["followup_id"])
        ticket_id = int(row["ticket_id"])

        conteudo = limpar_html(row.get("followup_content") or "")
        if len(conteudo) > 900:
            conteudo = conteudo[:900] + "..."

        grupos = row.get("grupos_nomes") or "Nao informado"
        link = f"{GLPI_URL_BASE}/front/ticket.form.php?id={ticket_id}"

        texto = (
            f"Requerente adicionou nota em chamado pendente\n\n"
            f"Grupos: {grupos}\n"
            f"Id: {ticket_id}\n"
            f"Titulo: {row.get('ticket_title')}\n"
            f"Followup ID: {followup_id}\n"
            f"Data: {row.get('followup_date')}\n"
            f"Conteudo:\n{conteudo or '(sem conteudo)'}\n\n"
            f"Link: {link}"
        )

        enviar_telegram(texto)

        if followup_id > last_id:
            last_id = followup_id

    state["last_followup_id"] = last_id


if __name__ == "__main__":
    print("Monitor iniciado")
    print(f"Intervalo: {INTERVALO} segundos")
    print(f"SLA percentual de alerta: {SLA_PERCENT_ALERTA}")

    while True:
        conn = None

        try:
            conn = conectar_db()
            cursor = conn.cursor()

            print("Processando SLA...")
            processar_sla(cursor)

            print("Processando followup do requerente...")
            processar_followup(cursor)

            salvar_estado(state)

        except Exception as e:
            print("ERRO:", e)

        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

        time.sleep(INTERVALO)