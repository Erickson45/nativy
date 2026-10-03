# Nativy

Nativy é uma plataforma de comunicação para quem trabalha na área de TI e precisa falar
inglês (ou outro idioma) sem dominar o idioma ainda. A ideia central: a pessoa fala no seu
próprio idioma durante uma call (entrevista de emprego, reunião de trabalho do dia a dia) e
uma IA transcreve, traduz e fala a tradução em tempo real para quem está do outro lado — e
o mesmo acontece no sentido inverso.

Este README documenta o estado atual do projeto, como rodar localmente, o que já funciona,
as limitações conhecidas e os próximos passos.

## Visão geral da arquitetura

Tudo roda localmente na máquina onde o servidor está (sem depender de serviços pagos de
IA), com a seguinte esteira de processamento por trecho de fala:

1. **Captura de áudio** — o navegador grava blocos curtos de áudio (atualmente ~3s) com a
   `MediaRecorder API`.
2. **Transcrição (Whisper)** — `faster-whisper` (modelo `base`, rodando em CPU) transcreve
   o áudio no idioma de origem, com filtro de VAD (detecção de atividade de voz) para
   ignorar silêncio/ruído.
3. **Tradução (Ollama)** — o texto transcrito é traduzido com um LLM local via Ollama
   (`llama3.2`), com um prompt que preserva jargão técnico de TI.
4. **Síntese de voz (gTTS)** — o texto traduzido vira áudio (mp3) com o Google TTS não
   oficial (`gTTS`), no sotaque escolhido pela pessoa que vai ouvir.
5. O áudio gerado e o texto (original + traduzido) voltam para o navegador via WebSocket.

Para as **calls** (duas pessoas reais, cada uma com seu navegador), o vídeo/áudio original
trafega **direto entre os dois navegadores por WebRTC** (peer-to-peer, com um servidor de
sinalização via WebSocket e STUN público para atravessar NAT). O servidor entra só para
processar o áudio e gerar a tradução — ele nunca vê o vídeo da chamada.

## Funcionalidades

- **Workspace** (`/tradutor`): tradução simultânea 1 pessoa só, útil pra testar/praticar
  frases antes de uma call de verdade.
- **Calls** (`/calls`): call real entre duas pessoas (WebRTC), com tradução ao vivo
  acontecendo em paralelo — cada lado fala no seu idioma e ouve a tradução no idioma do
  outro. Quando a tradução está ativa, o áudio original do convidado é abaixado para não
  sobrepor com a voz traduzida.
- **Histórico** (`/historico`): todas as transcrições/traduções da pessoa logada, com
  player de áudio.
- **Configurações de voz** (`/configuracoes`): escolha de sotaque/voz (via `tld` do gTTS)
  por idioma, e opção de fala mais devagar.
- **Login/cadastro** (`/login`, `/registrar`): conta de usuário simples (e-mail + senha),
  necessária pra usar qualquer parte acima — o histórico e as preferências de voz são por
  usuário.

## Como rodar localmente

Pré-requisitos:

- Python 3.11+ (recomendado)
- [Ollama](https://ollama.com) instalado e rodando, com o modelo baixado:
  ```bash
  ollama pull llama3.2
  ```
- FFmpeg instalado no sistema (o `faster-whisper`/Whisper precisa dele para ler os áudios
  webm enviados pelo navegador).

Instalação:

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

pip install -r requirements.txt
```

Rodar o servidor:

```bash
uvicorn main:app --reload
```

Acesse `http://localhost:8000`. Crie uma conta em `/registrar` antes de usar qualquer
funcionalidade — todas as páginas (exceto a inicial) exigem login.

Para testar uma **call** de verdade, abra duas abas (ou um navegador normal + uma aba
anônima, pra simular dois usuários/sessões diferentes): uma pessoa cria a sala em
`/calls`, copia o link gerado, e a outra entra com esse link/código em outra sessão.

Se aparecer no console do servidor um aviso de `VAD indisponível`, rode:

```bash
pip install onnxruntime
```

## Limitações conhecidas (estado atual)

- **Ainda não é tradução simultânea instantânea.** O áudio é processado em blocos (~3s)
  e depois passa por transcrição + tradução + síntese de voz — some mais alguns segundos
  de processamento em cima disso. Na prática, espere alguns segundos de atraso entre a
  fala original e a tradução, bem mais do que um intérprete humano profissional (2-4s).
  Reduzir isso de verdade exige trocar o corte fixo por segmentação baseada em pausa de
  fala e streaming parcial — ver Roadmap.
- **Sem servidor TURN.** A conexão WebRTC usa só STUN público. Isso funciona na maioria
  das redes domésticas, mas pode falhar atrás de firewalls corporativos mais restritivos
  (o cenário de "call de trabalho de verdade" que motivou o projeto). Um TURN server
  resolveria isso.
- **Limite de 2 pessoas por sala.** A conexão de vídeo/áudio é direta (mesh) entre os dois
  participantes. Para salas com mais gente seria necessário um SFU (servidor de mídia).
- **gTTS não é uma solução de produção.** É um wrapper não oficial do Google Translate,
  sem SLA, sujeito a bloqueio/rate limit se o uso crescer, e a voz é robótica. Serve bem
  para validar o produto agora.
- **Sessão de login em memória.** Se o processo do servidor reiniciar, todo mundo é
  deslogado. Para múltiplas instâncias do servidor em produção, seria necessário um store
  de sessão compartilhado (ex: Redis) em vez do dicionário em memória atual.
- **"Configurações de voz" ainda é escolha de sotaque, não clonagem de voz.** O pedido de
  "a IA falar com a minha própria voz" precisa de um motor de TTS diferente do gTTS (ver
  Roadmap) — o banco de dados já tem um campo (`voz_clonada_id`) preparado para isso.

## Roadmap sugerido

Em ordem de impacto no problema que o projeto resolve:

1. **Pipeline de latência baixa de verdade** — cortar os blocos de áudio por pausa de fala
   (VAD no navegador) em vez de um timer fixo, fazer transcrição incremental, e começar a
   gerar a voz traduzida assim que as primeiras palavras saírem da tradução, sem esperar a
   frase inteira.
2. **Servidor TURN** — garantir que a call funcione mesmo em redes corporativas
   restritivas, que é justamente o ambiente de uso real no trabalho.
3. **Clonagem de voz** — trocar o gTTS por um motor que suporte clonagem (ElevenLabs via
   nuvem — mais rápido e melhor qualidade, mas pago e envia a voz a terceiros; ou Coqui
   XTTS local — privado e gratuito, mas mais pesado e mais lento sem GPU). Essa é uma
   decisão de produto, não só técnica.
4. **Salas com mais de 2 pessoas** — exigiria um SFU (ex: LiveKit, mediasoup) no lugar do
   WebRTC mesh atual.
5. **Sessão de login persistente/compartilhada** e deploy em produção (HTTPS, banco de
   dados mais robusto que SQLite se o uso crescer, fila de processamento de áudio).

## Estrutura de pastas

```
main.py                  # Backend (FastAPI): rotas HTML, APIs, WebSockets
templates/                # Páginas (Jinja2)
  base.html                # Layout comum (nav, tema claro/escuro, login/logout)
  landing.html              # Página inicial pública
  login.html / registrar.html
  index.html                # Workspace (tradução 1 pessoa)
  calls.html                 # Calls (WebRTC + tradução ao vivo)
  historico.html
  configuracoes.html
static/                   # Arquivos estáticos
audios/                   # Áudios gerados (limpos automaticamente após 24h)
nativy.db                 # Banco SQLite (usuários, histórico, preferências de voz)
requirements.txt
```
