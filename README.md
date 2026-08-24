# Pipeline de Voos SIROS/ANAC

Pipeline de dados para monitorar voos em aeroportos brasileiros. O projeto consulta a API SIROS da ANAC, filtra e normaliza os registros, grava os dados no Supabase e disponibiliza um painel estático publicado no GitHub Pages.

Além dos voos previstos do dia, há uma carga mensal dos arquivos históricos VRA (Voo Regular Ativo) da ANAC. O painel permite consultar chegadas, partidas, histórico operacional e o status das últimas execuções do pipeline.

## Arquitetura

```text
API SIROS/ANAC ──┐
                 ├── GitHub Actions ──> scripts/fetch_flights.py ──> Supabase
Arquivos VRA ────┘                     scripts/fetch_historico_anac.py ──> Supabase
                                                                          │
                                                                          v
                                                               index.html / GitHub Pages
```

### Fluxo diário de voos

1. O workflow `Pipeline SIROS → Supabase` é executado quatro vezes por dia, às 06h, 09h, 12h e 18h no horário de Brasília.
2. `scripts/fetch_flights.py` consulta `https://sas.anac.gov.br/sas/siros_api/voos` usando a data atual como referência.
3. Os registros são filtrados pelos aeroportos configurados em `AIRPORTS`, normalizados e deduplicados.
4. Os dados são enviados em lotes de até 500 registros para a tabela `voos` usando `upsert`. A chave única evita duplicatas.
5. Cada execução é registrada na tabela `execucoes`, incluindo quantidade processada, lotes enviados, erros e status.

### Fluxo mensal do histórico

1. O workflow `Importar Histórico ANAC/VRA` é executado no dia 3 de cada mês, às 06h no horário de Brasília.
2. `scripts/fetch_historico_anac.py` baixa o arquivo VRA do mês anterior no portal de dados abertos da ANAC, com uma URL alternativa para contingência.
3. O CSV é lido em `latin-1`, filtrado pelos aeroportos, normalizado e deduplicado.
4. Os registros são gravados em lotes na tabela `historico_vra` usando `upsert`.
5. Uma execução manual pode receber `ANO_MES` no formato `AAAA-MM` para reprocessar um período específico.

### Painel

`index.html` é uma aplicação estática, sem etapa de build. Publicado pelo GitHub Pages, ele consulta diretamente a API REST do Supabase usando a chave pública `anon` e as políticas de leitura definidas em `sql/setup.sql`. O painel oferece:

- seleção de estado, município, aeroporto e data;
- tabelas de chegadas e partidas com filtros por companhia e tipo de operação;
- consulta do histórico VRA por aeroporto e mês;
- indicadores de execução e status do pipeline;
- atualização manual e automática dos voos selecionados.

## Estrutura de pastas

```text
.
├── index.html                         # Painel estático do GitHub Pages
├── data/
│   └── airports.json                  # Cadastro local de referência de aeroportos
├── scripts/
│   ├── fetch_flights.py               # Coleta diária do SIROS e upsert em voos
│   └── fetch_historico_anac.py        # Importação mensal do VRA/ANAC
├── sql/
│   └── setup.sql                      # Tabelas, índices, RLS, grants e aeroportos
├── .github/workflows/
│   ├── update-flights.yml             # Workflow diário do SIROS
│   └── importar-historico.yml         # Workflow mensal do histórico VRA
└── prints/                            # Capturas e material de apoio do projeto
```

O cadastro exibido pelo painel atualmente é lido da tabela `aeroportos` no Supabase. O arquivo `data/airports.json` permanece como referência local.

## Configuração do Supabase

1. Crie um projeto no [Supabase](https://supabase.com/).
2. Abra o **SQL Editor** e execute [`sql/setup.sql`](sql/setup.sql).
3. O script cria as tabelas `aeroportos`, `voos`, `execucoes` e `historico_vra`, além da view `voos_completo`, índices, RLS e permissões de leitura.
4. Em **Project Settings > API**, copie a URL do projeto e a chave `service_role` (ou Secret Key equivalente).

O painel precisa da URL e da chave pública `anon` em [`index.html`](index.html), nas constantes `SUPABASE_URL` e `SUPABASE_ANON_KEY`. A chave `anon` pode aparecer no código do frontend, pois o acesso fica limitado pelas políticas RLS. Nunca coloque `SUPABASE_SERVICE_KEY` no HTML, no `data/` ou em qualquer arquivo versionado.

## Variáveis de ambiente

### GitHub Actions

Configure os valores no repositório em **Settings > Secrets and variables > Actions**:

| Nome | Tipo | Obrigatório | Descrição |
| --- | --- | --- | --- |
| `SUPABASE_URL` | Secret | Sim | URL do projeto Supabase, por exemplo `https://seu-projeto.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Secret | Sim | Chave privada com permissão de escrita no Supabase |
| `AIRPORTS` | Variable | Não | Lista de códigos ICAO separados por vírgula, por exemplo `SBCA,SBCT,SBGR` |

Se `AIRPORTS` não for configurada, os workflows usam a lista de aeroportos definida como fallback no YAML. Se o script for executado diretamente sem essa variável, o padrão do código é `SBCA`.

O workflow mensal também aceita:

| Nome | Origem | Obrigatório | Descrição |
| --- | --- | --- | --- |
| `ANO_MES` | Input manual do workflow | Não | Período no formato `AAAA-MM`; vazio significa o mês anterior |

Para preencher `ANO_MES`, abra **Actions > Importar Histórico ANAC/VRA > Run workflow** e informe, por exemplo, `2026-04`.

### Execução local

No Windows PowerShell, defina as mesmas variáveis antes de executar um script:

```powershell
$env:SUPABASE_URL = "https://seu-projeto.supabase.co"
$env:SUPABASE_SERVICE_KEY = "sua-chave-privada"
$env:AIRPORTS = "SBCA,SBCT,SBGR"

pip install requests supabase
python scripts/fetch_flights.py
```

Para importar um mês específico:

```powershell
$env:ANO_MES = "2026-04"
python scripts/fetch_historico_anac.py
```

Os scripts usam Python 3.12 no GitHub Actions. Para execução local, recomenda-se Python 3.10 ou superior.

## Publicação no GitHub Pages

1. Faça o upload do projeto para um repositório no GitHub.
2. Confirme que `index.html` está na raiz da branch publicada.
3. Em **Settings > Pages**, selecione **Deploy from a branch**, escolha a branch principal e a pasta `/ (root)`.
4. Aguarde a publicação e acesse a URL informada pelo GitHub Pages.

Como o painel é estático, não é necessário Node.js, bundler ou servidor de aplicação. Ele só funcionará corretamente depois que o Supabase estiver configurado, o SQL tiver sido executado e as constantes públicas do Supabase em `index.html` apontarem para o projeto correto.

## Operação e manutenção

- Os workflows podem ser iniciados manualmente pela aba **Actions**.
- O workflow diário tem limite de 10 minutos; o histórico VRA tem limite de 30 minutos porque o CSV pode ser grande.
- Falhas parciais no envio dos lotes fazem o workflow diário terminar com erro e registram `erro_parcial` em `execucoes`.
- Falhas críticas de configuração, como ausência das credenciais, encerram o script antes da coleta.
- O horário armazenado pela API é tratado como UTC e exibido no painel em BRT (UTC-3).

## Fontes de dados

- [API SIROS/ANAC](https://sas.anac.gov.br/sas/siros_api/)
- [Dados abertos e arquivos VRA da ANAC](https://sistemas.anac.gov.br/dadosabertos/)
- [Supabase](https://supabase.com/)