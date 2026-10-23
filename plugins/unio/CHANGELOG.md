# Changelog

## 1.2.1

- As ferramentas do Unio passam a usar o token do workspace ligado à pasta da sessão. Antes, o Claude Code usava sempre o workspace padrão do computador, mesmo numa pasta de outro workspace.
- No Windows, com o Git for Windows, o aviso de início de sessão, /unio:doctor e a conexão com o Unio funcionam, e a página de aprovação abre sozinha no navegador.
- /unio:doctor confere se a conexão com o Unio vê o workspace desta pasta.

## 1.2.0

- Conecte sem copiar token: /unio:connect mostra um comando que pede a conexão ao Unio. Você confere o código, escolhe o workspace e aprova no navegador, e o token vai direto para este computador.
- O instalador faz o mesmo quando você o roda sem --workspace, ou com --browser.

## 1.1.0

- Um comando só instala o plugin e guarda o token: copie-o da página Configurações > Pessoal > Agente de código, no Unio.
- Cada workspace guarda o próprio token. O primeiro vale em todas as pastas; outro vale só na pasta onde você rodou o comando, a menos que você escolha o computador todo.
- /unio:connect leva a essa página e, quando a pasta já tem workspace, mostra o comando que troca o token.

## 1.0.0

- Primeira versão: a skill do Unio (1.0), o servidor MCP do Unio e os comandos /unio:connect, /unio:doctor, /unio:update e /unio:help.
- /unio:connect mostra como salvar o token num terminal seu, sem que ele passe pelo chat.
- No início da sessão, um aviso diz quando o Unio não está conectado, quando o token foi recusado ou está para vencer, e quando sai uma versão nova.
