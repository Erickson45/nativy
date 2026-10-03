from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import shutil
import os
import ollama
from faster_whisper import WhisperModel
from gtts import gTTS
import sqlite3
import uuid
import asyncio
import hashlib
import secrets
from datetime import datetime

app = FastAPI()

# Criamos a pasta audios para guardar as respostas geradas
os.makedirs("audios", exist_ok=True)

# Servimos a pasta audios para o HTML poder tocar os ficheiros
app.mount("/audios", StaticFiles(directory="audios"), name="audios")

templates = Jinja2Templates(directory="templates")

print("A carregar modelo de áudio local (Whisper)...")
modelo_whisper = WhisperModel("base", device="cpu", compute_type="int8")

IDIOMAS = {"pt": "Português", "en": "Inglês", "es": "Espanhol"}

SESSION_COOKIE = "nativy_session"

# Sotaques/voz disponíveis por idioma (usando o parâmetro tld do gTTS).
# Isto ainda não é clonagem de voz — é só a escolha de um sotaque/voz
# pronta. O campo voz_clonada_id já fica preparado no banco para quando
# decidirem o motor de clonagem de voz (ElevenLabs, Coqui, etc.).
OPCOES_SOTAQUE = {
    "pt": [("com.br", "Português do Brasil"), ("pt", "Português de Portugal")],
    "en": [
        ("com", "Inglês (Estados Unidos)"),
        ("co.uk", "Inglês (Reino Unido)"),
        ("com.au", "Inglês (Austrália)"),
        ("ca", "Inglês (Canadá)"),
    ],
    "es": [("com", "Espanhol (neutro)"), ("es", "Espanhol (Espanha)"), ("com.mx", "Espanhol (México)")],
}
TLD_PADRAO = {"pt": "com.br", "en": "com", "es": "com"}


# --- BANCO DE DADOS ---
def _garantir_coluna(cursor, tabela, coluna, tipo):
    """Adiciona uma coluna à tabela se ela ainda não existir (migração simples,
    pra não quebrar um nativy.db que já existia antes dessas mudanças)."""
    cursor.execute(f"PRAGMA table_info({tabela})")
    colunas_existentes = [linha[1] for linha in cursor.fetchall()]
    if coluna not in colunas_existentes:
        cursor.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")


def init_db():
    conn = sqlite3.connect("nativy.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            senha_salt TEXT NOT NULL,
            criado_em TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS configuracoes_voz (
            usuario_id INTEGER NOT NULL,
            idioma TEXT NOT NULL,
            tld TEXT NOT NULL,
            lento INTEGER NOT NULL DEFAULT 0,
            voz_clonada_id TEXT,
            PRIMARY KEY (usuario_id, idioma)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT,
            origem TEXT,
            destino TEXT,
            texto_origem TEXT,
            texto_destino TEXT,
            audio_file TEXT
        )
    """)
    # Migração: banco de dados antigo pode não ter a coluna usuario_id ainda.
    _garantir_coluna(cursor, "historico", "usuario_id", "INTEGER")

    conn.commit()
    conn.close()

init_db()

# --- GESTOR DE SALAS (MEMÓRIA) ---
salas_ativas = {}

class RoomConfig(BaseModel):
    max_users: int
    org_lang: str
    guest_lang: str
    room_name: str
    room_desc: str


# =====================================================================
# AUTENTICAÇÃO (sessão simples em memória — para múltiplos processos/
# deploy real, trocar por um store compartilhado tipo Redis)
# =====================================================================

sessoes: dict[str, int] = {}  # session_id -> usuario_id


def gerar_hash_senha(senha: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    hash_calculado = hashlib.pbkdf2_hmac(
        "sha256", senha.encode("utf-8"), bytes.fromhex(salt), 200_000
    ).hex()
    return hash_calculado, salt


def verificar_senha(senha: str, hash_salvo: str, salt: str) -> bool:
    hash_calculado, _ = gerar_hash_senha(senha, salt)
    return secrets.compare_digest(hash_calculado, hash_salvo)


def usuario_da_requisicao(request: Request) -> dict | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    usuario_id = sessoes.get(session_id)
    if not usuario_id:
        return None
    return obter_usuario_por_id(usuario_id)


def usuario_id_do_websocket(websocket: WebSocket) -> int | None:
    session_id = websocket.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    return sessoes.get(session_id)


def obter_usuario_por_id(usuario_id: int) -> dict | None:
    conn = sqlite3.connect("nativy.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, email FROM usuarios WHERE id = ?", (usuario_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def criar_sessao(resposta, usuario_id: int):
    session_id = secrets.token_hex(32)
    sessoes[session_id] = usuario_id
    resposta.set_cookie(
        SESSION_COOKIE, session_id, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30
    )
    return resposta


# =====================================================================
# HELPERS (rodam em thread separada via asyncio.to_thread para nunca
# bloquear o event loop do FastAPI enquanto processam um chunk de áudio)
# =====================================================================

def transcrever_audio(caminho_arquivo: str, idioma: str) -> str:
    """Transcreve um ficheiro de áudio com Whisper, usando VAD para ignorar
    silêncio/ruído (evita 'alucinações' do modelo em trechos sem fala)."""
    try:
        segments, _ = modelo_whisper.transcribe(
            caminho_arquivo,
            beam_size=5,
            language=idioma,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
    except Exception as e:
        # Se o filtro de VAD não estiver disponível no ambiente (ex: falta
        # onnxruntime), caímos de volta para a transcrição normal em vez
        # de quebrar o pipeline inteiro.
        print(f"[aviso] VAD indisponível ({e}), a transcrever sem filtro de silêncio.")
        segments, _ = modelo_whisper.transcribe(caminho_arquivo, beam_size=5, language=idioma)

    return " ".join(segment.text for segment in segments).strip()


def traduzir_texto(texto_original: str, origem_nome: str, destino_nome: str, tecnico: bool = True) -> str:
    """Chama o Ollama local para traduzir o texto transcrito."""
    if tecnico:
        prompt = (
            f"Você é um tradutor técnico de TI sênior. Traduza o texto a seguir de {origem_nome} "
            f"para {destino_nome}. Mantenha os jargões da área. Responda APENAS com a tradução, "
            f"sem aspas, notas ou introduções. Texto: {texto_original}"
        )
    else:
        prompt = (
            f"Traduza de {origem_nome} para {destino_nome}. "
            f"Apenas a tradução, sem aspas ou notas: {texto_original}"
        )

    resposta_ollama = ollama.chat(model='llama3.2', messages=[{'role': 'user', 'content': prompt}])
    return resposta_ollama['message']['content'].strip()


def gerar_audio(texto: str, idioma_destino: str, caminho_saida: str, tld: str | None = None, lento: bool = False) -> None:
    """Gera o áudio (TTS) da tradução, usando o sotaque/voz preferido de quem vai ouvir."""
    kwargs = {"text": texto, "lang": idioma_destino, "slow": lento}
    if tld:
        kwargs["tld"] = tld
    elif idioma_destino == "pt":
        kwargs["tld"] = "com.br"
    tts = gTTS(**kwargs)
    tts.save(caminho_saida)


def obter_preferencia_voz(usuario_id: int | None, idioma: str) -> dict:
    """Preferência de sotaque/voz de um usuário para um idioma. Se o usuário
    não configurou nada ainda (ou não está logado), usa o padrão."""
    padrao = {"tld": TLD_PADRAO.get(idioma, "com"), "lento": False}
    if not usuario_id:
        return padrao
    conn = sqlite3.connect("nativy.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT tld, lento FROM configuracoes_voz WHERE usuario_id = ? AND idioma = ?",
        (usuario_id, idioma),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"tld": row["tld"], "lento": bool(row["lento"])}
    return padrao


def obter_preferencias_usuario(usuario_id: int) -> dict:
    """Todas as preferências de voz do usuário, uma por idioma, com os
    idiomas ainda não configurados preenchidos com o padrão."""
    conn = sqlite3.connect("nativy.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT idioma, tld, lento FROM configuracoes_voz WHERE usuario_id = ?", (usuario_id,))
    preferencias = {row["idioma"]: {"tld": row["tld"], "lento": bool(row["lento"])} for row in cursor.fetchall()}
    conn.close()
    for idioma, tld_padrao in TLD_PADRAO.items():
        preferencias.setdefault(idioma, {"tld": tld_padrao, "lento": False})
    return preferencias


def salvar_historico(usuario_id: int | None, origem_nome: str, destino_nome: str, texto_original: str, texto_traduzido: str, nome_arquivo_audio: str | None) -> None:
    data_atual = datetime.now().strftime("%d/%m/%Y, %H:%M")
    conn = sqlite3.connect("nativy.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO historico (usuario_id, data_hora, origem, destino, texto_origem, texto_destino, audio_file)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (usuario_id, data_atual, origem_nome, destino_nome, texto_original, texto_traduzido, nome_arquivo_audio))
    conn.commit()
    conn.close()


# =====================================================================
# LIMPEZA PERIÓDICA DE ÁUDIOS ANTIGOS (uso diário contínuo não pode
# encher o disco de mp3 para sempre)
# =====================================================================

LIMPEZA_AUDIO_HORAS = 24
LIMPEZA_INTERVALO_SEGUNDOS = 3600


async def limpeza_periodica_audios():
    while True:
        await asyncio.sleep(LIMPEZA_INTERVALO_SEGUNDOS)
        try:
            agora = datetime.now().timestamp()
            limite_segundos = LIMPEZA_AUDIO_HORAS * 3600
            for nome_arquivo in os.listdir("audios"):
                caminho = os.path.join("audios", nome_arquivo)
                try:
                    if os.path.isfile(caminho) and (agora - os.path.getmtime(caminho)) > limite_segundos:
                        os.remove(caminho)
                except OSError:
                    pass
        except Exception as e:
            print(f"Erro na limpeza periódica de áudios: {e}")


@app.on_event("startup")
async def iniciar_tarefas_em_segundo_plano():
    asyncio.create_task(limpeza_periodica_audios())


# --- ROTAS DE AUTENTICAÇÃO ---
@app.get("/registrar", response_class=HTMLResponse)
async def get_registrar(request: Request):
    usuario = usuario_da_requisicao(request)
    if usuario:
        return RedirectResponse("/tradutor", status_code=303)
    return templates.TemplateResponse(request=request, name="registrar.html", context={"usuario": None, "erro": None})


@app.post("/registrar")
async def post_registrar(request: Request, nome: str = Form(...), email: str = Form(...), senha: str = Form(...)):
    email_normalizado = email.strip().lower()

    if len(senha) < 6:
        return templates.TemplateResponse(
            request=request, name="registrar.html",
            context={"usuario": None, "erro": "A senha precisa ter pelo menos 6 caracteres."},
            status_code=400,
        )

    conn = sqlite3.connect("nativy.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email_normalizado,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(
            request=request, name="registrar.html",
            context={"usuario": None, "erro": "Este e-mail já está cadastrado."},
            status_code=400,
        )

    hash_senha, salt = gerar_hash_senha(senha)
    cursor.execute(
        "INSERT INTO usuarios (nome, email, senha_hash, senha_salt, criado_em) VALUES (?, ?, ?, ?, ?)",
        (nome.strip(), email_normalizado, hash_senha, salt, datetime.now().strftime("%d/%m/%Y, %H:%M")),
    )
    conn.commit()
    usuario_id = cursor.lastrowid
    conn.close()

    resposta = RedirectResponse("/tradutor", status_code=303)
    return criar_sessao(resposta, usuario_id)


@app.get("/login", response_class=HTMLResponse)
async def get_login(request: Request, next: str = "/tradutor"):
    usuario = usuario_da_requisicao(request)
    if usuario:
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"usuario": None, "erro": None, "next": next})


@app.post("/login")
async def post_login(request: Request, email: str = Form(...), senha: str = Form(...), next: str = Form("/tradutor")):
    email_normalizado = email.strip().lower()

    conn = sqlite3.connect("nativy.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_normalizado,))
    row = cursor.fetchone()
    conn.close()

    if not row or not verificar_senha(senha, row["senha_hash"], row["senha_salt"]):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"usuario": None, "erro": "E-mail ou senha incorretos.", "next": next},
            status_code=401,
        )

    resposta = RedirectResponse(next or "/tradutor", status_code=303)
    return criar_sessao(resposta, row["id"])


@app.get("/logout")
async def get_logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id and session_id in sessoes:
        del sessoes[session_id]
    resposta = RedirectResponse("/login", status_code=303)
    resposta.delete_cookie(SESSION_COOKIE)
    return resposta


# --- ROTAS DE INTERFACE ---
@app.get("/", response_class=HTMLResponse)
async def get_landing(request: Request):
    usuario = usuario_da_requisicao(request)
    return templates.TemplateResponse(request=request, name="landing.html", context={"usuario": usuario})

@app.get("/tradutor", response_class=HTMLResponse)
async def get_interface(request: Request):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        return RedirectResponse("/login?next=/tradutor", status_code=303)
    return templates.TemplateResponse(request=request, name="index.html", context={"usuario": usuario})

@app.get("/historico", response_class=HTMLResponse)
async def get_historico(request: Request):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        return RedirectResponse("/login?next=/historico", status_code=303)

    conn = sqlite3.connect("nativy.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM historico WHERE usuario_id = ? ORDER BY id DESC", (usuario["id"],))
    dados_banco = cursor.fetchall()
    conn.close()

    return templates.TemplateResponse(
        request=request,
        name="historico.html",
        context={"historico": dados_banco, "usuario": usuario}
    )

@app.get("/calls", response_class=HTMLResponse)
async def get_calls(request: Request):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        return RedirectResponse("/login?next=/calls", status_code=303)
    return templates.TemplateResponse(request=request, name="calls.html", context={"usuario": usuario})


@app.get("/configuracoes", response_class=HTMLResponse)
async def get_configuracoes(request: Request):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        return RedirectResponse("/login?next=/configuracoes", status_code=303)
    preferencias = obter_preferencias_usuario(usuario["id"])
    return templates.TemplateResponse(
        request=request, name="configuracoes.html",
        context={"usuario": usuario, "preferencias": preferencias, "opcoes_sotaque": OPCOES_SOTAQUE}
    )


@app.post("/api/configuracoes")
async def post_configuracoes(request: Request, idioma: str = Form(...), tld: str = Form(...), lento: str = Form("nao")):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Não autenticado.")

    conn = sqlite3.connect("nativy.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO configuracoes_voz (usuario_id, idioma, tld, lento)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(usuario_id, idioma) DO UPDATE SET tld = excluded.tld, lento = excluded.lento
    """, (usuario["id"], idioma, tld, 1 if lento == "sim" else 0))
    conn.commit()
    conn.close()

    return RedirectResponse("/configuracoes", status_code=303)


# --- WEBSOCKET DO WORKSPACE (TRADUÇÃO CONTÍNUA COM GRAVAÇÃO NO HISTÓRICO) ---
@app.websocket("/ws/workspace/{source_lang}/{target_lang}")
async def websocket_workspace(websocket: WebSocket, source_lang: str, target_lang: str):
    usuario_id = usuario_id_do_websocket(websocket)
    if not usuario_id:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    origem_nome = IDIOMAS.get(source_lang, "Português")
    destino_nome = IDIOMAS.get(target_lang, "Inglês")

    try:
        while True:
            # Recebe o bloco de áudio em bytes de forma contínua
            audio_bytes = await websocket.receive_bytes()

            # Guarda o fragmento temporariamente para o Whisper ler
            temp_filename = f"temp_ws_work_{uuid.uuid4().hex}.webm"
            with open(temp_filename, "wb") as f:
                f.write(audio_bytes)

            try:
                # 1. Transcrição (Whisper) + 2. Tradução (Ollama) + 3. Voz (gTTS)
                # rodam em threads separadas para não travar o event loop.
                texto_original = await asyncio.to_thread(transcrever_audio, temp_filename, source_lang)

                if texto_original:
                    texto_traduzido = await asyncio.to_thread(
                        traduzir_texto, texto_original, origem_nome, destino_nome, True
                    )

                    # Aqui quem fala e quem ouve é a mesma pessoa, então usamos
                    # a preferência de voz dela mesma para o idioma de destino.
                    preferencia = await asyncio.to_thread(obter_preferencia_voz, usuario_id, target_lang)

                    nome_arquivo_audio = f"work_{uuid.uuid4().hex}.mp3"
                    caminho_audio = f"audios/{nome_arquivo_audio}"
                    await asyncio.to_thread(
                        gerar_audio, texto_traduzido, target_lang, caminho_audio, preferencia["tld"], preferencia["lento"]
                    )

                    # 4. Guardar no Banco de Dados (Para o Histórico funcionar)
                    await asyncio.to_thread(
                        salvar_historico, usuario_id, origem_nome, destino_nome, texto_original, texto_traduzido, nome_arquivo_audio
                    )

                    # 5. Envia o JSON de volta para a interface do Workspace
                    await websocket.send_json({
                        "texto_original": texto_original,
                        "texto_traduzido": texto_traduzido,
                        "audio_url": f"/audios/{nome_arquivo_audio}"
                    })
            except Exception as e:
                print(f"Erro no processamento do Workspace: {e}")
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)

    except WebSocketDisconnect:
        print("Utilizador desconectado do Workspace.")


# --- ROTA ANTIGA DO WORKSPACE (MANTIDA POR SEGURANÇA CASO QUEIRA VOLTAR) ---
@app.post("/translate/")
async def translate_audio(
    request: Request,
    audio: UploadFile = File(...),
    source_lang: str = Form(...),
    target_lang: str = Form(...)
):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Não autenticado.")

    temp_input = f"temp_{audio.filename}"
    with open(temp_input, "wb") as buffer:
        shutil.copyfileobj(audio.file, buffer)

    try:
        origem_nome = IDIOMAS.get(source_lang, "Português")
        destino_nome = IDIOMAS.get(target_lang, "Inglês")

        texto_original = await asyncio.to_thread(transcrever_audio, temp_input, source_lang)
        texto_traduzido = await asyncio.to_thread(traduzir_texto, texto_original, origem_nome, destino_nome, True)

        preferencia = await asyncio.to_thread(obter_preferencia_voz, usuario["id"], target_lang)

        nome_arquivo_audio = f"{uuid.uuid4().hex}.mp3"
        caminho_audio = f"audios/{nome_arquivo_audio}"
        await asyncio.to_thread(
            gerar_audio, texto_traduzido, target_lang, caminho_audio, preferencia["tld"], preferencia["lento"]
        )

        await asyncio.to_thread(
            salvar_historico, usuario["id"], origem_nome, destino_nome, texto_original, texto_traduzido, nome_arquivo_audio
        )

        return {
            "texto_original": texto_original,
            "texto_traduzido": texto_traduzido,
            "audio_url": f"/audios/{nome_arquivo_audio}"
        }
    finally:
        if os.path.exists(temp_input):
            os.remove(temp_input)


# --- ROTAS DA TELA DE CALLS (SALA E VALIDAÇÃO) ---
@app.post("/api/criar-sala")
async def criar_sala(config: RoomConfig, request: Request):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Não autenticado.")

    room_id = f"{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[4:8]}"

    salas_ativas[room_id] = {
        "room_name": config.room_name,
        "room_desc": config.room_desc,
        "max_users": config.max_users,
        "org_lang": config.org_lang,
        "guest_lang": config.guest_lang,
        "participantes": 0,
        "criada_em": datetime.now(),
    }

    # Usa o host real da requisição em vez de um domínio fixo, para
    # funcionar em qualquer máquina/porta onde o servidor estiver rodando.
    link = f"{request.base_url}calls?code={room_id}"
    return {"room_id": room_id, "link": link}

@app.get("/api/validar-sala/{room_id}")
async def validar_sala(room_id: str, request: Request):
    usuario = usuario_da_requisicao(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Não autenticado.")

    sala = salas_ativas.get(room_id)

    if not sala:
        raise HTTPException(status_code=404, detail="Sala não encontrada ou expirada.")

    if sala["participantes"] >= sala["max_users"]:
        raise HTTPException(status_code=403, detail="A sala já está lotada.")

    return {"status": "ok", "config": sala}


# =====================================================================
# WEBSOCKET DE SINALIZAÇÃO (WebRTC) — troca de offer/answer/ICE
# candidates entre os dois participantes de uma sala, para estabelecer
# uma conexão de áudio/vídeo peer-to-peer de verdade.
#
# Limite atual: 2 participantes por sala (ligação direta/mesh). Uma
# sala com mais convidados exigiria um SFU/servidor de mídia, o que
# fica fora do escopo desta etapa.
# =====================================================================
sinal_conexoes: dict[str, list[WebSocket]] = {}


@app.websocket("/ws/signal/{room_id}")
async def websocket_signal(websocket: WebSocket, room_id: str):
    if not usuario_id_do_websocket(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    conexoes_da_sala = sinal_conexoes.setdefault(room_id, [])

    if len(conexoes_da_sala) >= 2:
        await websocket.send_json({"type": "room-full"})
        await websocket.close()
        return

    sou_o_segundo_a_entrar = len(conexoes_da_sala) == 1
    conexoes_da_sala.append(websocket)

    sala = salas_ativas.get(room_id)
    if sala is not None:
        sala["participantes"] = len(conexoes_da_sala)

    # Quem chega primeiro fica esperando (responder); quem chega
    # segundo já sabe que tem alguém do outro lado e inicia a oferta.
    if sou_o_segundo_a_entrar:
        await websocket.send_json({"type": "role", "role": "initiator"})
        for outro in conexoes_da_sala:
            if outro is not websocket:
                await outro.send_json({"type": "peer-joined"})
    else:
        await websocket.send_json({"type": "role", "role": "responder"})

    try:
        while True:
            msg = await websocket.receive_json()
            # Apenas repassa a mensagem de sinalização para o outro
            # participante da sala (nunca para quem a enviou).
            for outro in sinal_conexoes.get(room_id, []):
                if outro is not websocket:
                    await outro.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Erro na sinalização da sala {room_id}: {e}")
    finally:
        conexoes_restantes = sinal_conexoes.get(room_id, [])
        if websocket in conexoes_restantes:
            conexoes_restantes.remove(websocket)
        if sala is not None:
            sala["participantes"] = len(conexoes_restantes)
        for outro in conexoes_restantes:
            try:
                await outro.send_json({"type": "peer-left"})
            except Exception:
                pass
        if not conexoes_restantes and room_id in sinal_conexoes:
            del sinal_conexoes[room_id]
        print(f"Utilizador desconectado da sinalização da sala {room_id}")


# =====================================================================
# WEBSOCKET PARA TRADUÇÃO EM TEMPO REAL NAS CALLS
#
# Cada participante abre a sua própria ligação com o seu par
# (source_lang -> target_lang). Quando a tradução de uma fala fica
# pronta, ela é enviada só para os OUTROS participantes da sala,
# nunca de volta para quem falou (antes ecoava a própria tradução),
# e o áudio é gerado com o sotaque/voz preferido de CADA ouvinte.
# =====================================================================
conexoes_ativas: dict[str, list[WebSocket]] = {}
conexoes_usuarios: dict[WebSocket, int] = {}

@app.websocket("/ws/translate/{room_id}/{source_lang}/{target_lang}")
async def websocket_translate(websocket: WebSocket, room_id: str, source_lang: str, target_lang: str):
    usuario_id = usuario_id_do_websocket(websocket)
    if not usuario_id:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    conexoes_ativas.setdefault(room_id, []).append(websocket)
    conexoes_usuarios[websocket] = usuario_id

    origem_nome = IDIOMAS.get(source_lang, "Português")
    destino_nome = IDIOMAS.get(target_lang, "Inglês")

    try:
        while True:
            audio_bytes = await websocket.receive_bytes()

            temp_filename = f"temp_ws_{uuid.uuid4().hex}.webm"
            with open(temp_filename, "wb") as f:
                f.write(audio_bytes)

            try:
                texto_original = await asyncio.to_thread(transcrever_audio, temp_filename, source_lang)

                if texto_original:
                    texto_traduzido = await asyncio.to_thread(
                        traduzir_texto, texto_original, origem_nome, destino_nome, False
                    )

                    # Só manda para quem NÃO foi quem falou — resolve o
                    # eco da própria fala traduzida voltando para si mesmo.
                    destinatarios = [c for c in conexoes_ativas.get(room_id, []) if c is not websocket]
                    ultimo_audio_gerado = None

                    for destino_ws in destinatarios:
                        ouvinte_id = conexoes_usuarios.get(destino_ws)
                        preferencia = await asyncio.to_thread(obter_preferencia_voz, ouvinte_id, target_lang)

                        nome_arquivo_audio = f"ws_{uuid.uuid4().hex}.mp3"
                        caminho_audio = f"audios/{nome_arquivo_audio}"
                        await asyncio.to_thread(
                            gerar_audio, texto_traduzido, target_lang, caminho_audio,
                            preferencia["tld"], preferencia["lento"]
                        )
                        ultimo_audio_gerado = nome_arquivo_audio

                        payload = {
                            "original": texto_original,
                            "traducao": texto_traduzido,
                            "audio_url": f"/audios/{nome_arquivo_audio}"
                        }
                        try:
                            await destino_ws.send_json(payload)
                        except Exception:
                            pass

                    await asyncio.to_thread(
                        salvar_historico, usuario_id, origem_nome, destino_nome,
                        texto_original, texto_traduzido, ultimo_audio_gerado
                    )
            except Exception as e:
                print(f"Erro no processamento do fragmento de áudio: {e}")
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)

    except WebSocketDisconnect:
        pass
    finally:
        if websocket in conexoes_ativas.get(room_id, []):
            conexoes_ativas[room_id].remove(websocket)
        conexoes_usuarios.pop(websocket, None)
        print(f"Utilizador desconectado da sala {room_id}")
