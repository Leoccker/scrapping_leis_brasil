# scrapping_leis_brasil

Baixa a legislação federal brasileira das fontes oficiais e transforma cada norma em
um **JSON estruturado** (metadados + artigos com hierarquia) e um **Markdown** legível.

Só usa a biblioteca padrão do Python (3.10+). Nada para instalar.

## Uso

```bash
python leis.py --tipos LEI,LCP --ano-ini 2020 --ano-fim 2020   # um recorte
```

```bash
python leis.py                                                  # tudo que é federal, de 1808 até hoje
```

Opções: `--out data`, `--tipos` (siglas: `LEI`, `LCP`, `LDL`, `DEL`, `MPV`, `DEC`, `DLG`, `EMC`, `CON`, `RSF`...;
vazio = todos), `--ano-ini`, `--ano-fim`, `--workers 4`, `--rps 4`, `--limit N`, `--retry-failed`.

A coleta é **retomável**: o progresso fica em `data/state.sqlite`; rodar o mesmo comando de novo
continua de onde parou. A coleta completa são centenas de milhares de normas e leva dias a 4 req/s —
respeite os servidores públicos e não aumente muito o `--rps`.

## Saída

```
data/json/<tipo>/<ano>/<norma>.json
data/md/<tipo>/<ano>/<norma>.md
```

Campos do JSON: `id`, `tipo`, `tipo_descricao`, `numero`, `ano`, `data`, `norma`, `nome`, `ementa`,
`apelidos`, `urn`, `url`, `palavras_chave`, `versao` (`compilada` = texto atualizado; `original` quando
não há compilação; `intermediaria` = só havia uma retificação; `null` = a fonte não tem o texto),
`versao_descricao`, `fonte_texto`, `licenca`, `baixado_em`, `preambulo`, `rodape`, `texto_integral`
e `artigos`:

```json
{
  "numero": "2º",
  "rotulo": "Art. 2º",
  "rubrica": null,
  "caminho": ["Capítulo I - DAS DISPOSIÇÕES GERAIS"],
  "texto": "Art. 2º A Administração Pública obedecerá...\nParágrafo único. ...\nI - ...",
  "revogado": false,
  "dispositivos": [
    {"tipo": "caput", "rotulo": "Art. 2º", "texto": "A Administração...", "revogado": false},
    {"tipo": "paragrafo", "rotulo": "Parágrafo único", "texto": "...", "revogado": false},
    {"tipo": "inciso", "rotulo": "I", "texto": "...", "revogado": false}
  ]
}
```

`artigos[].texto` é o artigo inteiro pronto para busca ou IA/RAG; `dispositivos` traz caput,
parágrafos, incisos, alíneas e itens na ordem do texto.

## Fontes e licença dos dados

- Listagem e URN: [Dados Abertos do Senado Federal](https://legis.senado.leg.br/dadosabertos)
- Metadados e texto: [normas.leg.br](https://normas.leg.br) (Congresso Nacional), licença CC-BY 4.0

Os textos **não substituem** a publicação no Diário Oficial da União.

## Limitações conhecidas

- Só legislação **federal** (estados e municípios têm portais próprios).
- Normas sem texto na fonte (muitos decretos antigos) saem só com metadados e ementa (`versao: null`).
- O parser é heurístico: anexos e tabelas ficam em `rodape`/`texto_integral`, não em `artigos`.

## Testes

```bash
python -m unittest
```
