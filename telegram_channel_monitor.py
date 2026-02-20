import os
import json
import logging
import asyncio
import requests
import psutil
import sys
import subprocess
from telethon import TelegramClient, events
from dotenv import load_dotenv

# Configuração de Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

from telethon.sessions import StringSession

# Carrega variáveis de ambiente
load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

# Novas variáveis para envio via Bot
token_env = os.getenv("ALERT_BOT_TOKEN")
ALERT_BOT_TOKEN = token_env.strip() if token_env else None
MY_TELEGRAM_ID = os.getenv("MY_TELEGRAM_ID")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_BASE64")

# Setup paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "monitor_config.json")
LOCK_FILE = os.path.join(BASE_DIR, "monitor_bot.lock")

def acquire_lock():
    """Garante que apenas uma instância do script esteja rodando"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                content = f.read().strip()
                if content:
                    pid = int(content)
                    if psutil.pid_exists(pid):
                        proc = psutil.Process(pid)
                        if "python" in proc.name().lower():
                            logger.warning(f"⚠️ Outra instância já está rodando (PID: {pid}). Encerrando esta.")
                            sys.exit(0)
        except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception as e:
            logger.error(f"Erro ao verificar lock: {e}")
    
    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.error(f"Erro ao criar lock file: {e}")

def release_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            logger.info("Lock file removido.")
        except:
            pass

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Erro ao ler config: {e}")
    return {
        "monitored_channels": [],
        "keywords": [],
        "excluded_keywords": []
    }

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        
        # Se estiver no GitHub Actions, tenta fazer o push das alterações
        if os.getenv("GITHUB_ACTIONS") == "true":
            push_to_github()
            
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar config: {e}")
        return False

def push_to_github():
    """Faz commit e push do arquivo de configuração para o repositório"""
    try:
        logger.info("📤 Sincronizando alterações com o GitHub...")
        
        # Configura usuário do Git (necessário para o commit)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        
        # Adiciona, commit e push
        subprocess.run(["git", "add", "monitor_config.json"], check=True)
        
        # Verifica se há algo para commitar para evitar erro
        status_result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        status = status_result.stdout
        
        if "monitor_config.json" in status:
            commit_result = subprocess.run(
                ["git", "commit", "-m", "🔄 Configuração atualizada via Bot [auto-save]"],
                capture_output=True, text=True
            )
            logger.info(f"Commit: {commit_result.stdout.strip()}")
            
            # Tenta evitar conflito trazendo alterações antes do push
            subprocess.run(["git", "pull", "--rebase"], capture_output=True, check=False)
            
            push_result = subprocess.run(
                ["git", "push"], capture_output=True, text=True
            )
            
            if push_result.returncode == 0:
                logger.info("✅ Configuração persistida no GitHub com sucesso!")
                send_via_bot("✅ <b>Git Push OK!</b> Configuração salva no repositório.")
            else:
                error_msg = push_result.stderr.strip()
                logger.error(f"❌ Git push falhou: {error_msg}")
                send_via_bot(f"❌ <b>Git Push FALHOU:</b>\n<code>{error_msg[:500]}</code>")
        else:
            logger.info("ℹ️ Nenhuma alteração pendente na configuração.")
            send_via_bot("ℹ️ Git: Nenhuma alteração detectada no arquivo.")
            
    except Exception as e:
        logger.error(f"❌ Falha ao sincronizar com GitHub: {e}")
        send_via_bot(f"❌ <b>Erro no Git:</b> {e}")

def send_via_bot(text):
    """Envia mensagem usando o Bot de Alerta via HTTP API"""
    if not ALERT_BOT_TOKEN or not MY_TELEGRAM_ID:
        logger.warning("ALERT_BOT_TOKEN ou MY_TELEGRAM_ID não configurados. Usando fallback.")
        return False
        
    url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            logger.error(f"Erro API Bot ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Exceção no envio via Bot API: {e}")
        return False

async def bot_command_handler():
    """Lê comandos enviados para o Bot de Alerta via Long Polling"""
    if not ALERT_BOT_TOKEN:
        return

    last_update_id = 0
    logger.info("📡 Escuta de comandos do Bot de Alerta iniciada.")

    while True:
        try:
            url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            resp = requests.get(url, params=params, timeout=35)
            
            if resp.status_code == 200:
                updates = resp.json().get("result", [])
                for update in updates:
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    chat_id = message.get("chat", {}).get("id")

                    # Somente aceita de quem é o dono
                    if str(chat_id) != str(MY_TELEGRAM_ID):
                        if chat_id:
                            logger.warning(f"⚠️ Comando recebido de ID não autorizado: {chat_id}.")
                        continue

                    # ========== MINI APP (web_app_data) ==========
                    web_app_data = message.get("web_app_data")
                    if web_app_data:
                        try:
                            raw_data = web_app_data.get("data", "{}")
                            data = json.loads(raw_data)
                            
                            # DEBUG 1: Confirma recebimento
                            to_add = data.get("add", [])
                            to_remove = data.get("remove", [])
                            send_via_bot(f"🔍 <b>Mini App recebido:</b>\n+{len(to_add)} adds, -{len(to_remove)} removes\nGITHUB_ACTIONS={os.getenv('GITHUB_ACTIONS')}")
                            logger.info(f"Mini App dados: add={to_add}, remove={to_remove}")
                            
                            config = load_config()
                            updated = False
                            summary = []

                            if data.get("action") == "sync_config":
                                added = []
                                for t in to_add:
                                    if t not in config["keywords"]:
                                        config["keywords"].append(t)
                                        added.append(t)
                                if added:
                                    summary.append(f"✅ Adicionados: {', '.join(added)}")
                                    updated = True

                                removed = []
                                for raw_t in to_remove:
                                    t = raw_t.replace("🚫 ", "") if raw_t.startswith("🚫 ") else raw_t
                                    
                                    if t in config["keywords"]:
                                        config["keywords"].remove(t)
                                        removed.append(t)
                                    elif t in config["excluded_keywords"]:
                                        config["excluded_keywords"].remove(t)
                                        removed.append(f"🚫 {t}")
                                        
                                if removed:
                                    summary.append(f"❌ Removidos: {', '.join(removed)}")
                                    updated = True

                                if updated:
                                    # DEBUG 2: Antes de salvar
                                    send_via_bot(f"💾 Salvando config... ({len(config['keywords'])} keywords)")
                                    if save_config(config):
                                        msg = "📱 <b>Painel Atualizado:</b>\n\n" + "\n".join(summary)
                                        send_via_bot(msg)
                                        logger.info(f"Sincronização via Mini App: +{added} -{removed}")
                                    else:
                                        send_via_bot("❌ Erro ao salvar configurações do Mini App.")
                                else:
                                    send_via_bot("ℹ️ Nenhuma alteração real foi necessária (tokens já atualizados).")
                        except Exception as e:
                            logger.error(f"Erro ao processar dados do Mini App: {e}")
                            send_via_bot(f"❌ Erro ao ler dados do painel: {e}")
                        continue

                    # ========== COMANDOS DE TEXTO ==========
                    text = message.get("text", "")

                    if not text.startswith("/"):
                        continue

                    parts = text.split()
                    cmd = parts[0].lower()
                    arg = parts[1].upper() if len(parts) > 1 else ""

                    config = load_config()
                    response = ""

                    if cmd == "/insert":
                        if arg:
                            if arg not in config["keywords"]:
                                config["keywords"].append(arg)
                                if save_config(config):
                                    response = f"✅ Token <b>{arg}</b> adicionado ao monitoramento."
                                else:
                                    response = "❌ Erro ao salvar configuração."
                            else:
                                response = f"ℹ️ Token <b>{arg}</b> já está na lista."
                        else:
                            response = "⚠️ Uso: /insert [TOKEN]"

                    elif cmd == "/remove":
                        if arg:
                            if arg in config["keywords"]:
                                config["keywords"].remove(arg)
                                if save_config(config):
                                    response = f"✅ Token <b>{arg}</b> removido do monitoramento."
                                else:
                                    response = "❌ Erro ao salvar configuração."
                            else:
                                response = f"⚠️ Token <b>{arg}</b> não encontrado na lista."
                        else:
                            response = "⚠️ Uso: /remove [TOKEN]"

                    elif cmd == "/exclude":
                        if arg:
                            if arg not in config["excluded_keywords"]:
                                config["excluded_keywords"].append(arg)
                                if save_config(config):
                                    response = f"✅ Palavra <b>{arg}</b> adicionada à lista de exclusão."
                                else:
                                    response = "❌ Erro ao salvar configuração."
                            else:
                                response = f"ℹ️ Palavra <b>{arg}</b> já está excluída."
                        else:
                            response = "⚠️ Uso: /exclude [PALAVRA]"

                    elif cmd == "/include":
                        if arg:
                            if arg in config["excluded_keywords"]:
                                config["excluded_keywords"].remove(arg)
                                if save_config(config):
                                    response = f"✅ Palavra <b>{arg}</b> removida da lista de exclusão (voltará a ser monitorada)."
                                else:
                                    response = "❌ Erro ao salvar configuração."
                            else:
                                response = f"⚠️ Palavra <b>{arg}</b> não encontrada na lista de exclusão."
                        else:
                            response = "⚠️ Uso: /include [PALAVRA]"
                    
                    elif cmd == "/status":
                        is_gh = os.getenv("GITHUB_ACTIONS") == "true"
                        kw_count = len(config["keywords"])
                        response = (
                            f"📊 <b>Status do Bot</b>\n\n"
                            f"🖥️ Ambiente: {'GitHub Actions' if is_gh else 'Local'}\n"
                            f"📋 Keywords: {kw_count}\n"
                            f"🚫 Excluídas: {len(config['excluded_keywords'])}\n"
                            f"📡 Canais: {config.get('monitored_channels', [])}\n"
                            f"🔑 Session: {'StringSession' if is_gh else 'Local file'}"
                        )

                    elif cmd == "/list":
                        kw_list = ", ".join(config["keywords"])
                        ex_list = ", ".join(config["excluded_keywords"])
                        response = f"📋 <b>Monitoramento Atual</b>\n\n<b>Keywords:</b>\n{kw_list}\n\n<b>Excluídas:</b>\n{ex_list}"

                    elif cmd == "/painel":
                        try:
                            painel_url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
                            painel_payload = {
                                "chat_id": MY_TELEGRAM_ID,
                                "text": "📱 Toque no botão abaixo para abrir o painel:",
                                "reply_markup": json.dumps({
                                    "keyboard": [[{
                                        "text": "📱 Abrir Painel Olheiro",
                                        "web_app": {"url": "https://rahulgusmao.github.io/olheiro-criptos/"}
                                    }]],
                                    "resize_keyboard": True,
                                    "one_time_keyboard": True
                                })
                            }
                            requests.post(painel_url, json=painel_payload, timeout=10)
                            logger.info("📱 Botão do Painel enviado ao usuário.")
                        except Exception as e:
                            logger.error(f"Erro ao enviar botão do painel: {e}")
                        continue

                    if response:
                        send_via_bot(response)

        except Exception as e:
            logger.error(f"Erro no polling de comandos: {e}")
            await asyncio.sleep(5)
        
        await asyncio.sleep(1)

# Variável global para armazenar o client e facilitar acesso em handlers externos
client_instance = None

async def on_web_app_data(event):
    """Recebe dados enviados pelo Mini App"""
    try:
        data = json.loads(event.data)
        config = load_config()
        updated = False
        summary = []

        if data.get("action") == "sync_config":
            to_add = data.get("add", [])
            to_remove = data.get("remove", [])
            
            # Processa Inclusões
            added = []
            for t in to_add:
                if t not in config["keywords"]:
                    config["keywords"].append(t)
                    added.append(t)
            if added:
                summary.append(f"✅ Adicionados: {', '.join(added)}")
                updated = True

            # Processa Remoções
            removed = []
            for t in to_remove:
                if t in config["keywords"]:
                    config["keywords"].remove(t)
                    removed.append(t)
            if removed:
                summary.append(f"❌ Removidos: {', '.join(removed)}")
                updated = True
            
            if updated:
                if save_config(config):
                    msg = "📱 <b>Painel Atualizado:</b>\n\n" + "\n".join(summary)
                    send_via_bot(msg)
                    logger.info(f"Sincronização via Mini App concluída: +{added} -{removed}")
                else:
                    send_via_bot("❌ Erro ao salvar configurações enviadas pelo Mini App.")
            else:
                send_via_bot("ℹ️ Nenhuma alteração real foi necessária.")
                
    except Exception as e:
        logger.error(f"Erro ao processar dados do Mini App: {e}")
        send_via_bot(f"❌ Erro ao ler dados do painel: {e}")

async def main():
    # Garante instância única
    acquire_lock()
    
    # Validação inicial
    if not API_ID or not API_HASH:
        logger.critical("ERRO CRÍTICO: TELEGRAM_API_ID ou TELEGRAM_API_HASH ausentes no .env")
        return

    if not ALERT_BOT_TOKEN or not MY_TELEGRAM_ID:
        logger.warning("⚠️ AVISO: Configurações do Bot de Alerta ausentes.")
    else:
        logger.info("✅ Configuração de Bot de Alerta detectada.")

    # Inicia a escuta de comandos em segundo plano
    asyncio.create_task(bot_command_handler())

    try:
        while True:
            try:
                config = load_config()
                # Sessões separadas: GitHub usa StringSession, Local usa arquivo próprio
                if os.getenv("GITHUB_ACTIONS") == "true":
                    if not SESSION_STRING:
                        logger.critical("❌ SESSION_STRING ausente no GitHub Actions!")
                        return
                    session = StringSession(SESSION_STRING)
                    logger.info("🔑 Usando StringSession (GitHub Actions)")
                else:
                    session = "monitor_session_local"
                    logger.info("🔑 Usando sessão local (monitor_session_local)")
                
                client = TelegramClient(session, int(API_ID), API_HASH)
                
                @client.on(events.NewMessage(chats=config.get("monitored_channels", [])))
                async def handler(event):
                    try:
                        message_text = event.message.message
                        if not message_text:
                            return

                        current_config = load_config()
                        keywords = current_config.get("keywords", [])
                        matched = [kw for kw in keywords if kw.lower() in message_text.lower()]
                        
                        if matched:
                            excluded = current_config.get("excluded_keywords", [])
                            if any(ex.lower() in message_text.lower() for ex in excluded):
                                logger.info(f"Ignorado (palavra excluída): {matched}")
                                return

                            logger.info(f"🔥 KEYWORD MATCH: {matched}")
                            full_message = message_text
                            
                            if not send_via_bot(full_message):
                                try:
                                    await client.send_message('me', full_message)
                                except Exception: pass
                            else:
                                logger.info("Alerta enviado com sucesso via Bot!")
                                
                    except Exception as e:
                        logger.error(f"Erro no handler: {e}")

                @client.on(events.NewMessage())
                async def web_app_handler(event):
                    try:
                        # Verifica se é uma mensagem de serviço com dados de WebView
                        if hasattr(event.message, 'action'):
                            action = event.message.action
                            # Verifica se a ação tem o atributo 'text' e data (típico de MessageActionWebViewDataSent)
                            if hasattr(action, 'text') and getattr(type(action), '__name__', '') == 'MessageActionWebViewDataSent':
                                # Simula estrutura para função existente
                                class MockEvent:
                                    def __init__(self, data):
                                        self.data = data
                                await on_web_app_data(MockEvent(action.text))
                    except Exception as e:
                        logger.error(f"Erro no web_app_handler: {e}")

                logger.info(f"Monitorando em canais: {config.get('monitored_channels', [])}")
                await client.start()
                await client.run_until_disconnected()
                
            except Exception as e:
                logger.error(f"ERRO DE CONEXÃO OU CRASH: {e}")
                logger.info("Reiniciando em 10 segundos...")
                await asyncio.sleep(10)
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário.")
    finally:
        release_lock()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
