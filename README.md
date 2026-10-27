<h1>TSDK</h1>

<h3>Plataforma para criar jogos do T-TEA</h3>

<p align="justify">
  O TSDK é a base dos jogos do <a href="https://github.com/larvattea/ttea">T-TEA</a>, o console de exergames de chão interativo.
  O jogo cuida só das regras e do desenho. O TSDK cuida da janela, da câmera, do rastreamento e da calibração.
</p>

<p align="justify">
  Cada jogo vira um cartucho <code>.ttea</code>, que o T-TEA instala e executa.
  Os jogos dependem apenas do <code>tsdk</code>.
</p>

## Desenvolvimento

### Requisitos

- [uv](https://docs.astral.sh/uv/). Ele baixa o Python 3.14 e as dependências sozinho.
- Windows x86-64, Linux x86-64 ou macOS ARM, as plataformas testadas no CI.

O uv instala apenas pacotes pré-compilados, então não é preciso compilador nem Visual Studio.

### Instalar o uv

macOS:

```bash
brew install uv
```

Windows (PowerShell):

```powershell
winget install --id=astral-sh.uv -e
```

Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Criar um jogo em seis comandos

Deixe o `tsdk` e os jogos na mesma pasta e rode:

```bash
cd tsdk
uv sync --locked
uv run tsdk new ../meu-jogo --profile body
cd ../meu-jogo
uv lock
uv run tsdk dev
```

O `--profile` escolhe o rastreamento: `body` (corpo), `hands` (mãos) ou `face` (rosto).
O projeto novo baixa o `tsdk` do GitHub. Para usar a cópia local, mude a fonte no `pyproject.toml`: `tsdk = { path = "../tsdk", editable = true }`.
Faça commit do `uv.lock` de cada jogo.

### Cinco comandos cobrem todo o ciclo

| Comando | O que faz |
| --- | --- |
| `tsdk new DESTINO [--id ID] [--name NOME] [--profile body\|hands\|face]` | Cria o projeto: `ttea.toml`, `pyproject.toml`, `game.py`, `.gitignore` e `LICENSE` |
| `tsdk check [projeto]` | Valida o manifesto e o código sem executar o jogo |
| `tsdk dev [projeto]` | Abre o modo de desenvolvimento (precisa do extra `tsdk[dev]`, com o PySide6) |
| `tsdk build [projeto] [--output ARQUIVO] [--release]` | Gera `dist/<id>-<versão>.ttea` |
| `tsdk play CARTUCHO --lang IDIOMA --player_id ID --professional_id ID --data-dir PASTA` | Executa um cartucho no console T-TEA |

### O jogo é uma classe com seis métodos

O TSDK chama `start`, `update`, `draw`, `pause`, `resume` e `stop` a cada etapa do jogo.
Herde de `tsdk.api.Game` e implemente só os métodos que usar:

```python
import pygame
from tsdk.api import Game, GameContext


class MeuJogo(Game):
    def start(self, context: GameContext):
        self.segundos = 0.0

    def update(self, context: GameContext):
        self.segundos += context.dt  # o TSDK não chama update com o jogo pausado

    def draw(self, context: GameContext):
        context.surface.fill((20, 25, 35))
        largura, altura = context.surface.get_size()
        for pe in context.tracking.feet.values():
            pygame.draw.circle(context.surface, (80, 220, 160), (round(pe.x * largura), round(pe.y * altura)), 12)

    def stop(self, context: GameContext):
        return {"segundos_ativos": self.segundos}  # salvo como resultado da sessão
```

- `context.tracking.feet` traz os pés em coordenadas de 0 a 1 sobre a tela do jogo.
- Leia teclado e mouse em `context.events`, não em `pygame.mouse` ou `pygame.key`.
- Para botões no chão, use `FloorSelector` e `feet_centre`: o jogador escolhe ficando 2 s sobre o botão.
- Para encerrar, chame `context.request_stop(resultado)`.
- Se o jogo desenha o próprio jogador, defina `show_feet = False` na classe para esconder as pegadas.

### O cartucho `.ttea` guarda o jogo inteiro

O cartucho é um ZIP com o código, os recursos (`assets`) e as licenças listados no `ttea.toml`.
Você escreve o `ttea.toml`; este é o do Ginga Divina:

```toml
[game]
id = "gingadivina"
version = "2.0.0"
entrypoint = "game.main:GingaDivina"
tracking_profile = "body"   # body, hands ou face
resolution = [800, 600]     # opcional; padrão 800x600 (4:3)
game_api = 1
requires_tsdk = ">=0.2,<0.3"
assets = ["assets"]
licenses = ["LICENSE", "CREDITS.md"]

[game.name]
pt = "Ginga Divina"
en = "Ginga Divina"
```

O `tsdk build` gera o `manifest.json` a partir dele, com a versão do TSDK e o commit ([Apêndice](#apêndice-campos-que-o-build-adiciona)).

### O modo de desenvolvimento testa o jogo sem o console

`uv run tsdk dev [projeto]` abre uma janela em tela cheia, ainda sem jogo.
Ele usa o PySide6, que fica no extra `dev`: o `uv sync` no `tsdk` e os projetos criados por `tsdk new` (`tsdk[dev]`) já o instalam. O console T-TEA só executa `tsdk play` e não baixa o PySide6.
Em **Load game**, escolha uma pasta de projeto ou um `.ttea`. Cartuchos na pasta `games/` do TSDK aparecem direto no menu.
O jogo ocupa dois terços da tela; o terço da direita controla o rastreamento, a gravação e o jogo.

| Fonte de rastreamento | Como move o jogador |
| --- | --- |
| Mouse | Arraste o esqueleto na prévia para mover os dois pés |
| Teclado | W A S D movem o pé esquerdo; as setas, o pé direito |
| Câmera | **Both hands**: cada palma move um pé. **Face (nose)**: o nariz move o centro dos pés |

Mouse e teclado dispensam câmera, cadastro e calibração. A webcam só abre ao escolher **Camera**.

Por padrão, o jogador é anônimo e `context.records` vale `None`.
Marque **Registered player** para testar configurações por jogador, salvas em `<pasta de dados>/tsdk/players/<id do jogo>.json`.
**Edit config…** edita esse JSON e reinicia o jogo; **Reset** apaga o arquivo.
A pasta de dados é `%LOCALAPPDATA%` no Windows, `~/.local/share` no Linux e `~/Library/Application Support` no macOS.

### O T-TEA executa o cartucho com `tsdk play`

Instale o `tsdk` uma vez no computador do console:

```bash
uv tool install --python 3.14 git+https://github.com/larvattea/tsdk
```

O T-TEA (branch `developer-mode`) lista os `.ttea` da pasta de jogos e executa cada um assim:

```bash
tsdk play jogo.ttea --lang pt_BR --player_id 7 --professional_id 3 --data-dir "<pasta de dados do T-TEA>"
```

O `tsdk play` valida o cartucho, confere o `requires_tsdk` e lê a calibração do T-TEA sem alterá-la:

| Arquivo em `<data-dir>/calibration/` | Uso |
| --- | --- |
| `calibration_hardware.ini` | Qual câmera usar, e qual tela é o projetor e seu tamanho |
| `calibration_point.csv` | Os quatro cantos do chão projetado |

O jogo ocupa a tela do projetor, e o operador vê a câmera com o esqueleto em outra janela.
Os dados ficam em `<data-dir>/tsdk/<id do jogo>/`.
O comando sai com 0 ao fim normal, e com 1 para cartucho inválido ou incompatível ou calibração ausente.

### A versão final exige um Git limpo

Faça commit de tudo, deixe `dist/` no `.gitignore` e rode:

```bash
uv run tsdk build --release --output dist/jogo.ttea
```

O build falha com arquivos modificados ou fora do Git. Todo arquivo empacotado precisa estar versionado, inclusive recursos que o `.gitignore` ignora.

### O `tsdk check` aplica as regras do T-TEA

| Regra | O que exige |
| --- | --- |
| Dependências | O `pyproject.toml` lista só o `tsdk`, com a mesma faixa de versão do `requires_tsdk` |
| Imports | Biblioteca padrão, `pygame`, `numpy`, `tsdk.api`, `tsdk.tracking` e os módulos do jogo |
| Chamadas | Sem janela, eventos, relógio, câmera, `subprocess`, `eval`/`exec` ou instalação de pacotes |
| Arquivos | Sem links, arquivos ocultos, binários, modelos de ML ou bibliotecas da plataforma |
| Ciclo de vida | A classe de entrada tem os seis métodos com `(self, context)`, ou herda de `tsdk.api.Game` |

Essas regras são convenções, não um sandbox de segurança.

### Os jogos reutilizam um CI pronto

```yaml
jobs:
  tsdk:
    uses: larvattea/tsdk/.github/workflows/game.yml@main
    with:
      builder-ref: main
    secrets: inherit
```

No Windows, Linux e macOS, o CI valida o jogo, roda os testes e gera o cartucho.
Depois, instala e executa o cartucho, simulando perda e retorno do rastreamento.
Câmera, projetor, áudio e latência ainda precisam de teste no hardware real.

| Entrada | Padrão | Uso |
| --- | --- | --- |
| `builder-ref` | obrigatório | Ref do TSDK; fixe um commit revisado |
| `project-path` | `.` | Pasta do jogo no repositório |
| `python-versions` | `'["3.14"]'` | Versões do Python testadas, em JSON |
| `release` | `false` | Gera com `--release` |

### Apêndice: campos que o build adiciona

O jogo não pode definir estes campos do `manifest.json`:

| Campo | Valor |
| --- | --- |
| `format_version` | `1` |
| `builder.version` | Versão instalada do `tsdk` |
| `build.source_commit` | `HEAD` do Git, ou `"unversioned"` fora do Git |
| `build.dirty` | `true` se houver arquivos modificados ou fora do Git |
