# Preço Certo

Aplicação pessoal para cadastrar produtos uma única vez e preservar pesquisas de preços ao longo do tempo. Interface em português, responsiva para computador e celular.

## Iniciar

Requisitos: Docker Desktop em execução, com contêineres Linux, e PowerShell 7.

Na pasta do projeto, execute:

```powershell
pwsh -File scripts/iniciar.ps1
```

Abra http://localhost:8088. O script cria `.env` com uma senha aleatória para o banco. Não compartilhe esse arquivo. A primeira execução baixa imagens e dependências.

Para parar: `docker compose stop`. Para iniciar novamente sem reconstruir: `docker compose up -d --wait`.

## Uso

1. Em **Preferências**, informe CEP, pagamentos aceitos e interesse em importados/usados.
2. Em **Cadastrar produto**, informe marca, modelo, características, condição, objetivo de preço e fotos opcionais.
3. Ao cadastrar ou salvar um produto ativo, uma pesquisa é adicionada à fila. O coletor consulta catálogos públicos e o cartão atualiza sem recarregar a página. Para produtos anteriores à atualização, clique em **Pesquisar agora**.
4. Em **Ver histórico → Registrar manualmente**, você ainda pode registrar ofertas com data, loja, link, pagamento e valores.
5. O sistema calcula o total como **preço + frete − desconto**. Para importados, informe o preço já com todos os tributos. O frete deve corresponder ao CEP de entrega.
6. Consulte o gráfico de **preço sem frete**, o menor preço registrado e a diferença, em reais e percentual, entre a última pesquisa e o menor preço das pesquisas anteriores. A primeira coleta aparece como **Primeiro registro**, sem comparação fictícia. Uma pesquisa inserida com data antiga ocupa sua posição cronológica.
7. O botão **Ver produto na loja** abre a oferta em nova aba. Em **Ver histórico → Onde comprar**, há o menor preço da última pesquisa por loja, com variação e link direto. A tabela mantém o link de cada oferta histórica; preço antigo pode ter mudado.
8. Use **Exportar para Excel** para baixar um `.xlsx` com Produtos, Ofertas e Preferências. Fotos ficam no armazenamento da aplicação e no backup, não no Excel. Datas no Excel têm fuso UTC explícito; a interface usa o fuso do navegador.

As preferências são orientações para pesquisa: não excluem ofertas antigas nem impedem registrar outras condições/pagamentos. Pesquisas e ofertas são preservadas sem edição nesta versão. Para um preço atualizado, registre uma nova pesquisa. Produtos podem ser editados ou arquivados, mantendo seus registros.

### Editar um produto cadastrado

No cartão, clique em **Editar produto**. O formulário abre com as informações salvas, incluindo nome, marca, modelo, categoria, características, condição e preço desejado. Para calçados ou roupas, use **Tamanho desejado** (um tamanho) e **Cores aceitas** (alternativas separadas por vírgula). Deixe vazio para aceitar qualquer tamanho/cor. A prévia mostra os termos da pesquisa e informa se o CEP está definido nas preferências, sem duplicar o endereço no produto.

**Salvar e pesquisar** atualiza o mesmo produto e solicita a coleta. Não cria outro cadastro nem apaga fotos ou ofertas anteriores. Alterar tamanho/cores inicia uma nova revisão de comparação, como já ocorre para modelo, condição e características. O catálogo valida a variação de cada SKU; a busca web só inclui no histórico preços de páginas que confirmem o tamanho e uma das cores aceitas. Uma variação não confirmada pode aparecer como página para conferir, fora do histórico. Produtos arquivados podem ser editados, mas precisam ser reativados para pesquisar. O frete é consultado nos catálogos compatíveis; consulte as regras de monitoramento abaixo.

### Coleta pública gratuita

A pesquisa automática consulta os catálogos públicos VTEX das lojas configuradas em `collector.py`: Mizuno (somente para essa marca), Decathlon e Carrefour. Na validação real do Neo Aura Knit, a Mizuno respondeu e as outras duas lojas recusaram a consulta. A tela informa resultados parciais e falhas por loja; não há garantia de acesso permanente nem cobertura de toda a internet. Não há tentativas de contornar bloqueios.

### Ampliar a descoberta de lojas

Em **Preferências → Busca ampliada em outras lojas**, crie uma conta Free da [SerpApi](https://serpapi.com/pricing), copie a chave no campo protegido e clique em **Validar e ativar**. Não envie a chave pelo chat. Depois clique em **Pesquisar agora**. O provedor anuncia 250 buscas mensais no plano gratuito na data da implementação; confira as condições atuais na página dele. O sistema não cria conta, não contrata planos e não ativa renovação paga.

A conta e a cota são verificadas pela Account API, que não consome a cota de buscas. A aplicação aceita apenas plano identificado como Free com preço mensal zero e cota disponível. Cada pesquisa usa até cinco consultas: busca geral (até 20 páginas), Google Shopping e resolução dos anúncios mais baratos de até três lojas distintas. A resolução é reduzida quando a cota restante é pequena. O provedor recebe os termos de marca/modelo e, na busca geral, tamanho e cores; não recebe CEP, fotos nem a base de preços. A chave fica em `/data/search-api-key`, com permissão restrita ao usuário da aplicação, no volume persistente. A API local nunca devolve seu valor. **Desativar** remove a chave. Ela é excluída do backup de dados/fotos; após restaurar em outra instalação, configure-a novamente.

O Shopping descobre o modelo sem exigir tamanho no título, pois anúncios podem omitir a variação. Os anúncios aparecem em uma seção própria com **preço anunciado**, sem confirmação de tamanho, cor, pagamento ou frete. Eles não alteram o histórico até a validação na página da loja. Quando o provedor entrega apenas um link intermediário do Google, a interface o identifica como **Ver anúncio no Google**; quando resolve o endereço da mesma loja, mostra **Conferir na loja**. Um preço de outro vendedor não é atribuído à loja do anúncio original.

O cartão informa a quantidade de lojas com preços confirmados na pesquisa mais recente. Sete tamanhos/cores de uma única loja contam como **uma loja**, não sete comparações. Se apenas uma loja respondeu, há um aviso de comparação limitada. Anúncios com preços inferiores são sinalizados como pendentes de confirmação, sem substituir silenciosamente o preço histórico.

O mecanismo usa os resultados orgânicos para encontrar links diretos de lojas, descartando categorias, buscas e agregadores conhecidos. As páginas são lidas para confirmar marca, modelo, preço em reais, condição nova e estoque positivo em dados estruturados Product/Offer. Valores de snippets ou AggregateOffer não são usados. Quando o preço é confirmado mas a forma de pagamento não está na página, aparece **Confirmar na loja**. Uma página bloqueada ou sem dados suficientes aparece em **Mais lojas para conferir**, com link e aviso, fora do histórico de preços. Isso não garante autenticidade da loja, entrega no CEP, correspondência de todas as variações ou cobertura completa da internet.

Sem chave, são tentados dois índices públicos gratuitos, Bing RSS e DuckDuckGo HTML. No teste real eles não forneceram páginas adicionais úteis; a interface informa a limitação. O link **Ampliar no Google Shopping** permite continuar a pesquisa no navegador, sem gravar resultados dessa navegação automaticamente.

O coletor exige os termos do modelo e a marca corretos, estoque positivo e pelo menos uma forma de pagamento explicitamente associada ao preço e aceita nas preferências. Produtos usados/recondicionados não são coletados por esses catálogos. Importados identificados são excluídos quando a preferência não permite; origem ausente é exibida como **Origem não informada**. As características obrigatórias usam correspondência conservadora de termos no catálogo: prefira `Azul 40` a uma frase longa. Uma característica não confirmada exclui a oferta.

Sem tamanho/cor nas características obrigatórias, a pesquisa compara todas as variações disponíveis; cada oferta mostra sua variação e o link com SKU. A consulta limita-se a 50 produtos por loja e 150 ofertas por loja, com aviso quando houver truncamento. O gráfico compara **preço menos desconto, sem frete**. Os cartões e alertas consideram o total entregue confirmado. Quando não confirmados, frete e total ficam **A calcular**, nunca zero. No Excel, essas células ficam vazias. A disponibilidade e o preço devem ser confirmados na loja.

**O histórico começa nas consultas registradas pelo sistema. Os catálogos não fornecem histórico anterior ao cadastro.** Cada coleta guarda fonte, data, ofertas e resultado de cada loja. Uma falha ou busca vazia não cria uma oferta de valor zero e não apaga pesquisas anteriores. A fila é persistida no PostgreSQL e processada pelo serviço `worker`, inclusive com o navegador fechado. Repetições em menos de 60 segundos reutilizam a consulta recente; trabalhos interrompidos são retomados após cinco minutos.

## Monitoramento e Telegram

A consulta automática ocorre às 9h de Brasília: dias 1–14 sem coleta automática; dias 15–20 com API e fontes públicas; dias 21 até o fim do mês apenas fontes públicas e links já encontrados. Consultas manuais continuam disponíveis, mas também usam a API somente nos dias 15–20. O computador, Docker e serviço worker precisam estar ligados. Ao iniciar depois das 9h, o sistema recupera a consulta do dia, sem executar consultas dos dias anteriores.

Cada trabalho reserva conservadoramente até cinco consultas SerpApi: orçamento local de 200 unidades para trabalhos automáticos e 250 no total do mês, preservando 50 para consultas manuais. Reservas não representam consumo faturado e não descontam uso externo da mesma chave. O provedor continua validando a disponibilidade da conta gratuita. Produtos alternam a prioridade diária quando o orçamento é limitado.

**Quero pagar representa produto + frete + taxas − desconto.** Nos catálogos compatíveis, o sistema simula a entrega dos dez primeiros SKUs de cada loja para o CEP informado, sem criar pedido. Só aceita frete se estoque, SKU, vendedor e preço forem confirmados. Onde a loja bloquear ou não fornecer esses dados, o total permanece pendente e não dispara alerta. O gráfico continua mostrando preço do produto sem frete; a meta e os cartões usam total confirmado para o CEP e pagamento aceito.

Em **Alertas e Telegram**, siga os passos: crie um bot exclusivo com `/newbot` no @BotFather, cole o token no sistema, prepare a conexão, abra o link gerado e toque em Iniciar. Volte para Confirmar vínculo e use Enviar mensagem de teste. Não envie o token pelo chat. O vínculo aceita apenas uma conversa privada iniciada pelo link temporário.

Os avisos incluem link direto e são gerados quando um total confirmado atinge a meta ou fica abaixo do menor total anterior comparável. O primeiro registro acima da meta não gera aviso. Ofertas iguais não são repetidas; falhas de envio têm até três tentativas. Alertas anteriores ao vínculo não são enviados retroativamente. A configuração fica em `/data/telegram.json`, com acesso restrito, no volume persistente. Assim como a chave SerpApi e o `.env`, esse segredo não entra no backup de banco/fotos; após uma restauração em outro volume, configure-o novamente.

Alterar marca, modelo, características obrigatórias ou condição inicia uma nova revisão para a comparação. As ofertas anteriores continuam disponíveis na tabela e no Excel, sem misturar preços de outro modelo/variação no gráfico atual. Alterar apenas nome, categoria ou objetivo de preço mantém a revisão.

## Armazenamento e backup

PostgreSQL guarda produtos, pesquisas, ofertas e preferências no volume `postgres_data`. Fotos são convertidas para JPEG e guardadas no volume `photos`. Valores monetários são inteiros em centavos. Recriar contêineres mantém esses volumes. **Não execute `docker compose down -v` para parar o sistema: isso apaga os volumes.**

Crie uma cópia completa:

```powershell
pwsh -File scripts/backup.ps1
```

O script pausa brevemente a aplicação para copiar um banco consistente e as fotos para `backups/AAAAMMDD-HHMMSS/`, depois reinicia a aplicação. Guarde a pasta inteira em outro disco ou armazenamento externo. Não há agendamento de backup na versão inicial. Excel serve para consulta; não substitui esse backup.

### Restaurar em uma instalação vazia

Faça isso apenas no destino que deve receber o backup. Copie a pasta de backup para o projeto e substitua `PASTA` nos comandos pelo nome real. A restauração sobrescreve os registros da instalação de destino; guarde uma cópia dela antes.

```powershell
docker compose stop worker app
docker compose cp backups/PASTA/database.dump db:/tmp/restore.dump
docker compose exec -T db pg_restore -U preco -d preco --clean --if-exists --no-owner --exit-on-error /tmp/restore.dump
docker compose cp backups/PASTA/photos/. app:/data/photos/
docker compose run --rm --user root app chown -R app:app /data/photos
docker compose start app worker
```

## Acesso pelo celular

O padrão expõe somente em `127.0.0.1`. Para uso na sua rede privada de confiança, adicione `BIND_ADDRESS=0.0.0.0` ao `.env` e rode `docker compose up -d`. Libere a porta 8088 no firewall somente para rede privada. No celular conectado ao mesmo Wi-Fi, acesse `http://IP-DO-COMPUTADOR:8088`.

A aplicação é de uso pessoal e **não possui login nem HTTPS** nesta versão. Quem acessar a porta poderá ler e alterar os dados. Para acesso pela internet, implemente autenticação e HTTPS ou use uma VPN privada; não encaminhe essa porta diretamente no roteador.

## Estrutura

- `app.py`: API Flask, PostgreSQL, validações, fotos e Excel.
- `collector.py`: leitura dos catálogos públicos, correspondência e filtro de ofertas.
- `discovery.py`: descoberta de páginas de outras lojas, validação e leitura limitada de dados estruturados; bloqueia conexões a redes privadas e valida os destinos de redirecionamentos.
- `shopping.py`: consulta Google Shopping, resolve links de até três lojas e mantém preços anunciados separados dos preços confirmados.
- `provider_settings.py`: validação da conta gratuita e persistência restrita da chave do provedor.
- `worker.py`: processamento da fila persistente de consultas.
- `static/`: interface HTML/CSS/JavaScript sem dependências de frontend ou CDNs.
- `compose.yaml`: aplicação Gunicorn e PostgreSQL, verificação de saúde e volumes.
- `tests/test_app.py`: testes de integração em banco separado `preco_test`.
- `scripts/`: inicialização e backup.

## Testes

Com os serviços iniciados, crie um banco exclusivo de testes (somente uma vez):

```powershell
docker compose exec -T db createdb -U preco preco_test
docker compose exec -T -e DB_NAME=preco_test -e UPLOAD_DIR=/tmp/preco-test-photos app python -m unittest discover -s tests -v
```

Os testes limpam apenas tabelas do banco `preco_test`, nunca o banco de uso `preco`. Cobrem histórico cronológico, pesquisas com várias lojas, centavos, validação, arquivamento, preferências, fotos e conteúdo real do Excel.

## Referências de implementação

- [Transações Psycopg](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
- [Uploads e limites no Flask](https://flask.palletsprojects.com/en/stable/patterns/fileuploads/)
- [API pública de pesquisa VTEX](https://developers.vtex.com/docs/api-reference/search-api)
- [API de pesquisa SerpApi](https://serpapi.com/search-api)
- [Account API SerpApi](https://serpapi.com/account-api)
