# Despesas fixas: regra de domínio

`FixedExpense` é a fonte oficial do compromisso provisionado. Suas versões são
imutáveis por competência passada. `CashMovement` continua sendo o ledger da
movimentação realizada no Caixa e pode apontar para `fixed_expense_id` quando for
o pagamento daquele compromisso.

## Resultado disponível

- A provisão vigente é deduzida uma vez, independentemente de ter sido paga.
- Um `CashMovement` vinculado é exibido como pagamento realizado, mas não é
  deduzido novamente.
- Um `CashMovement` histórico com categoria FIXED e sem vínculo é fallback
  legado: continua deduzido separadamente enquanto não existir provisão da mesma
  categoria e unidade no período. Se existir, a provisão oficial substitui esse fallback;
  assim o pagamento antigo não duplica o compromisso.
- Saídas manuais novas aceitam categoria VARIABLE. Categoria FIXED exige o
  vínculo explícito com uma despesa fixa.

A fórmula gerencial mistura fontes deliberadamente: receitas, comissões e taxas
são realizadas/snapshot; impostos e despesas fixas são provisionados; custos
variáveis são saídas realizadas. A interface nomeia as parcelas provisionadas.

## Categoria e histórico

`financial_category_id` é a fonte estruturada atual. Cada
`FixedExpenseVersion` também guarda `category_name_snapshot`, apenas para preservar
o texto exibido no histórico. Categorias em uso não podem mudar de natureza nem
ser excluídas.

Criação, edição e ativação/inativação geram `AuditLog`. Criação e mudanças
com `effective_from` anterior ao mês corrente são rejeitadas; não existe
retroatividade silenciosa.

## Unidade e permissões

Nesta versão toda despesa pertence a uma unidade (`branch_id NOT NULL`). Uma
despesa corporativa deve ser cadastrada/alocada por unidade; suporte nativo a
rateio fica para uma decisão futura, evitando semântica implícita agora.

- Alíquotas e escrita de categorias: `organization.manage` (configuração).
- Leitura de categorias: `finance.view` (necessária nas telas financeiras).
- Despesas fixas: `finance.view` para leitura e `finance.manage` para mutação.
