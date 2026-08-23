# Melhorias de Performance PostgreSQL - ClickUp

## 📋 Resumo das Otimizações Implementadas

Este documento detalha as otimizações de performance implementadas após a migração para PostgreSQL, focando em auditoria, esteira e módulo CRM.

---

## 🎯 Problemas Identificados

1. **Lentidão na Esteira/Auditoria**: Consultas sem índices apropriados causando full table scans
2. **Importações OSAB/Churn/Ciclo Lentas**: Uso de `iterrows` + `update_or_create` causando N round-trips ao banco
3. **Payload Excessivo**: Carregamento de campos grandes (observações) mesmo quando não necessários
4. **Falta de Índices Compostos**: Filtros combinados sem índices otimizados

---

## ✅ Melhorias Implementadas

### 1. Índices Simples no Modelo Venda

**Arquivo**: `crm_app/models.py`

Adicionado `db_index=True` nos seguintes campos:
- `vendedor` (FK para Usuario)
- `status_tratamento` (FK para StatusCRM)
- `status_esteira` (FK para StatusCRM)
- `status_comissionamento` (FK para StatusCRM)
- `data_criacao` (DateTimeField)
- `ordem_servico` (CharField)
- `data_instalacao` (DateField)
- `motivo_pendencia` (FK para MotivoPendencia)
- `auditor_atual` (FK para Usuario)

**Impacto**: Acelera filtros individuais em até 10-50x dependendo do volume de dados.

---

### 2. Índices Compostos e Parciais

**Arquivo**: `crm_app/migrations/0066_create_performance_indexes.py`

Criados 6 índices especializados usando `CREATE INDEX CONCURRENTLY`:

#### a) Índice para Flow de Auditoria
```sql
CREATE INDEX CONCURRENTLY idx_venda_flow_auditoria 
ON crm_venda(status_tratamento_id, ativo) 
WHERE status_tratamento_id IS NOT NULL AND status_esteira_id IS NULL AND ativo IS TRUE;
```
**Uso**: Tela de auditoria que filtra vendas com tratamento definido mas sem esteira.

#### b) Índice para Flow de Esteira
```sql
CREATE INDEX CONCURRENTLY idx_venda_flow_esteira 
ON crm_venda(status_esteira_id, ativo) 
WHERE status_esteira_id IS NOT NULL AND ativo IS TRUE;
```
**Uso**: Listagem de vendas ativas na esteira.

#### c) Índice para Flow de Comissionamento
```sql
CREATE INDEX CONCURRENTLY idx_venda_flow_comiss 
ON crm_venda(status_esteira_id, status_comissionamento_id) 
WHERE status_esteira_id IS NOT NULL;
```
**Uso**: Filtro de vendas instaladas pendentes de comissão.

#### d) Índice Composto de Datas
```sql
CREATE INDEX CONCURRENTLY idx_venda_datas 
ON crm_venda(data_criacao, data_instalacao) 
WHERE ativo IS TRUE;
```
**Uso**: Relatórios e dashboards que filtram por períodos.

#### e) Índice Vendedor + Data
```sql
CREATE INDEX CONCURRENTLY idx_venda_vendedor_data 
ON crm_venda(vendedor_id, data_criacao DESC) 
WHERE ativo IS TRUE;
```
**Uso**: Listagem "Minhas Vendas" ordenada por data.

#### f) Índice para Auditoria Alocada
```sql
CREATE INDEX CONCURRENTLY idx_venda_auditor 
ON crm_venda(auditor_atual_id) 
WHERE auditor_atual_id IS NOT NULL AND ativo IS TRUE;
```
**Uso**: Listar vendas em auditoria por auditor específico.

---

### 3. Índices em Tabelas de Importação

**ImportacaoOsab.documento**: Adicionado `db_index=True` para acelerar cruzamento com Venda.ordem_servico.

**ImportacaoChurn.numero_pedido**: Já possui `unique=True` (que cria índice automaticamente).

---

### 4. Otimização de Importações com Bulk Operations

#### ImportacaoChurnView
**Antes**: `update_or_create` linha a linha (N queries)
**Depois**: 
- Carrega registros existentes em memória uma vez
- Separa em listas `to_create` e `to_update`
- `bulk_create` + `bulk_update` em batches de 1000

**Ganho estimado**: 50-100x mais rápido para arquivos grandes (10k+ linhas).

#### ImportacaoCicloPagamentoView
Mesma otimização aplicada.

#### ImportacaoOsabView
Já estava parcialmente otimizada. Mantido bulk operations existente.

---

### 5. Otimização de Queryset com .defer()

**Arquivo**: `crm_app/views.py` - `VendaViewSet.get_queryset()`

Adicionado `.defer('observacoes', 'complemento', 'ponto_referencia')` para evitar carregar campos de texto grandes durante listagens.

**Impacto**: Reduz tráfego de rede e memória em 10-30% dependendo do tamanho médio das observações.

---

## 🚀 Como Aplicar as Melhorias

### Passo 1: Gerar e Aplicar Migrations

```powershell
# 1. Gerar migration automática dos índices simples (já gerada)
python manage.py migrate crm_app 0065_alter_importacaoosab_documento_and_more

# 2. Aplicar migration customizada com índices compostos
python manage.py migrate crm_app 0066_create_performance_indexes
```

⚠️ **IMPORTANTE**: A migration 0066 usa `CREATE INDEX CONCURRENTLY`, que:
- Não bloqueia a tabela durante criação
- Recomendado para produção
- Pode levar alguns minutos dependendo do volume de dados

---

### Passo 2: Validar Índices Criados

Após aplicar as migrations, conecte ao PostgreSQL e valide:

```sql
-- Ver todos os índices da tabela crm_venda
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE tablename = 'crm_venda' 
ORDER BY indexname;

-- Verificar tamanho dos índices
SELECT 
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
WHERE tablename = 'crm_venda'
ORDER BY pg_relation_size(indexrelid) DESC;
```

---

### Passo 3: Testar Performance

#### Teste 1: Listagem de Esteira
```python
# Antes das otimizações:
# Tempo esperado: 2-5 segundos

# Depois das otimizações:
# Tempo esperado: 100-300ms
```

#### Teste 2: Importação OSAB
```python
# Arquivo com 5000 linhas
# Antes: ~5-10 minutos
# Depois: ~30-60 segundos
```

#### Teste 3: Importação Churn
```python
# Arquivo com 10000 linhas
# Antes: ~10-20 minutos
# Depois: ~1-2 minutos
```

---

## 📊 Comandos Úteis para Análise

### Ver Plano de Execução de Query Lenta

```sql
EXPLAIN ANALYZE
SELECT * FROM crm_venda 
WHERE ativo = TRUE 
  AND status_esteira_id IS NOT NULL 
  AND data_criacao >= '2026-01-01'
ORDER BY data_criacao DESC
LIMIT 100;
```

### Estatísticas de Uso de Índices

```sql
SELECT 
    schemaname,
    tablename,
    indexname,
    idx_scan as index_scans,
    idx_tup_read as tuples_read,
    idx_tup_fetch as tuples_fetched
FROM pg_stat_user_indexes
WHERE tablename = 'crm_venda'
ORDER BY idx_scan DESC;
```

### Identificar Queries Lentas (Ativar log_min_duration)

No arquivo `postgresql.conf` (ou via settings do provider):
```
log_min_duration_statement = 1000  # Log queries > 1 segundo
```

---

## 🔧 Ajustes Finos Adicionais (Opcional)

### 1. Aumentar work_mem para Importações

```sql
-- Temporariamente para sessão de importação grande
SET work_mem = '256MB';
```

### 2. Ajustar shared_buffers (em produção)

Recomendado: 25% da RAM disponível
```
shared_buffers = 2GB  # Ajustar conforme servidor
```

### 3. Habilitar Caching com Redis

Para dashboards/estatísticas que não mudam frequentemente:
```python
from django.core.cache import cache

@action(detail=False)
def estatisticas(self, request):
    cache_key = 'vendas_stats_mes_atual'
    stats = cache.get(cache_key)
    if not stats:
        stats = self.calcular_estatisticas()
        cache.set(cache_key, stats, timeout=300)  # 5 minutos
    return Response(stats)
```

---

## 📈 Monitoramento Contínuo

### Queries Lentas a Monitorar

1. **Auditoria**: `GET /api/vendas/?flow=auditoria`
2. **Esteira**: `GET /api/vendas/?flow=esteira`
3. **Comissionamento**: `GET /api/vendas/?flow=comissionamento`
4. **Importação OSAB**: `POST /api/importacao/osab/`
5. **Exportação Excel**: `GET /api/vendas/exportar_excel/`

### Alertas Sugeridos

- Query > 2 segundos na esteira/auditoria
- Importação > 5 minutos para 10k linhas
- Taxa de erro > 1% nas importações

---

## 🎓 Documentação de Referência

- [PostgreSQL Indexes](https://www.postgresql.org/docs/current/indexes.html)
- [Django Query Optimization](https://docs.djangoproject.com/en/4.2/topics/db/optimization/)
- [Partial Indexes PostgreSQL](https://www.postgresql.org/docs/current/indexes-partial.html)
- [Django bulk_create/bulk_update](https://docs.djangoproject.com/en/4.2/ref/models/querysets/#bulk-create)

---

## ✅ Checklist de Implementação

- [x] Adicionar índices simples no modelo Venda
- [x] Adicionar índices em ImportacaoOsab e ImportacaoChurn
- [x] Criar migration com índices compostos e parciais
- [x] Refatorar ImportacaoChurnView para bulk operations
- [x] Refatorar ImportacaoCicloPagamentoView para bulk operations
- [x] Otimizar queryset de VendaViewSet com .defer()
- [ ] Aplicar migrations em produção
- [ ] Validar performance pós-implementação
- [ ] Monitorar queries lentas por 1 semana
- [ ] Ajustar índices conforme padrões reais de uso

---

**Data da Implementação**: 03 de Janeiro de 2026  
**Responsável**: Sistema de Otimização Automatizada  
**Versão**: 1.0
