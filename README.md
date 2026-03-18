# LicitaPro — versão atualizada

## Novidades desta versão

### 1) Exclusão de licitações
- botão para excluir licitação individual
- exclusão em lote na tela de licitações
- confirmação antes de apagar
- exclusão em cascata dos itens vinculados

### 2) Licitações ganhas
- nova área **Licitações Ganhas**
- status de acompanhamento:
  - Em habilitação
  - Em adjudicação
  - Homologado
- campo de observações do acompanhamento
- filtro por status

### 3) Uso mais rápido no dia a dia
- novo **Modo Rápido**
- foco em: referência, valor encontrado, custo máximo e status
- área grande “**Até quanto posso ir**”
- botão de copiar valor
- menos cliques e leitura mais imediata

### 4) Tela de itens mais prática
- cards por item em vez de depender só de tabela
- leitura objetiva por item
- ações rápidas: foco, editar, duplicar, excluir, copiar valor
- exclusão em lote de itens mantida

## Regra do sistema

**Custo máximo permitido = valor de referência ÷ 2**

## Como iniciar

### Primeira vez
No terminal da pasta do projeto:

```powershell
py -m pip install -r requirements.txt
```

### Uso diário
Basta abrir:

- `start.bat`

ou rodar:

```powershell
py app.py
```

## Login inicial
- e-mail: `admin@licitacao.local`
- senha: `admin123`

## Observação técnica
Esta versão adiciona campos novos para acompanhamento de licitações ganhas. O sistema tenta ajustar automaticamente a estrutura do banco SQLite existente na inicialização.
