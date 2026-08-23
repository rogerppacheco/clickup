# ✅ Checklist Final - Deploy em Produção

## 🔒 PRÉ-REQUISITOS CRÍTICOS

### 1. Backup do Banco de Dados ⚠️
- [ ] **OBRIGATÓRIO**: Fazer backup completo do PostgreSQL
- [ ] Testar restauração do backup (se possível)
- [ ] Guardar backup em local seguro

### 2. Verificar Ambiente
- [ ] Confirmar que está usando **PostgreSQL** (não SQLite)
- [ ] Verificar espaço em disco (índices ocupam ~10-20% da tabela)
- [ ] Confirmar que pode haver 5-15 minutos de criação de índices

### 3. Janela de Manutenção
- [ ] Escolher horário de baixo movimento (noite/madrugada)
- [ ] Avisar equipe sobre manutenção
- [ ] Ter plano de rollback pronto

---

## 🚀 PROCEDIMENTO DE DEPLOY

### Passo 1: Backup
```bash
# PostgreSQL via pg_dump
pg_dump -U usuario -h host -d database > backup_antes_performance_$(date +%Y%m%d_%H%M%S).sql

# OU via painel do provedor (AWS RDS, Azure, etc)
```

### Passo 2: Commit e Push
```powershell
# Verificar mudanças
git status

# Adicionar arquivos
git add crm_app/models.py
git add crm_app/views.py
git add crm_app/migrations/
git add docs/
git add scripts/
git add *.md

# Commit
git commit -m "feat: Otimizações de performance PostgreSQL

- Adicionar índices no modelo Venda
- Implementar bulk operations nas importações
- Adicionar índices compostos e parciais
- Otimizar queries com .defer()

Ganhos esperados:
- Queries: 10-50x mais rápidas
- Importações: 50-100x mais rápidas"

# Push
git push origin main
```

### Passo 3: Deploy no Servidor
```bash
# Conectar ao servidor
ssh usuario@servidor

# Ir para diretório do projeto
cd /caminho/do/projeto

# Pull das mudanças
git pull origin main

# Ativar ambiente virtual (se usar)
source venv/bin/activate

# Aplicar migrations
python manage.py migrate crm_app

# Aguardar conclusão (5-15 minutos)
# Você verá: "✓ Índices de performance PostgreSQL criados com sucesso!"
```

### Passo 4: Validação
```bash
# Executar script de validação
python scripts/validar_performance.py

# Verificar se todos os índices foram criados
# Confirmar tempos de resposta < 500ms
```

### Passo 5: Restart da Aplicação
```bash
# Gunicorn
sudo systemctl restart gunicorn

# OU Supervisor
sudo supervisorctl restart site-clickup

# OU Docker
docker-compose restart web

# Verificar logs
tail -f /var/log/gunicorn/error.log
```

### Passo 6: Testes de Fumaça
- [ ] Acessar página de login
- [ ] Navegar para esteira (deve estar < 500ms)
- [ ] Navegar para auditoria (deve estar < 500ms)
- [ ] Fazer busca por OS
- [ ] Testar filtro por data
- [ ] Fazer importação OSAB pequena (teste)

---

## 📊 MONITORAMENTO PÓS-DEPLOY

### Primeira Hora
- [ ] Monitorar logs de erro
- [ ] Verificar tempo de resposta das APIs
- [ ] Confirmar que não há erros 500
- [ ] Testar importações

### Primeiro Dia
- [ ] Coletar feedback da equipe
- [ ] Verificar métricas de performance
- [ ] Validar tempos de importação

### Primeira Semana
- [ ] Analisar uso dos índices
- [ ] Identificar queries ainda lentas
- [ ] Ajustar se necessário

---

## 🔄 PLANO DE ROLLBACK (SE NECESSÁRIO)

Se algo der errado:

### Opção 1: Reverter Migration
```bash
# Reverter para migration anterior
python manage.py migrate crm_app 0064

# Restart
sudo systemctl restart gunicorn
```

### Opção 2: Restaurar Backup
```bash
# Parar aplicação
sudo systemctl stop gunicorn

# Restaurar banco
psql -U usuario -h host -d database < backup_antes_performance_*.sql

# Reverter código
git revert HEAD

# Restart
sudo systemctl start gunicorn
```

---

## 📈 MÉTRICAS ESPERADAS

### Antes das Otimizações
- Esteira: 2-5 segundos
- Auditoria: 2-5 segundos
- Importação OSAB (5k): 5-10 minutos
- Importação Churn (10k): 10-20 minutos

### Depois das Otimizações
- Esteira: **100-300ms** (10-50x mais rápido) ⚡
- Auditoria: **100-300ms** (10-50x mais rápido) ⚡
- Importação OSAB (5k): **30-60s** (10x mais rápido) 🚀
- Importação Churn (10k): **1-2min** (10-20x mais rápido) 🚀

---

## 🆘 TROUBLESHOOTING

### Erro: "CREATE INDEX CONCURRENTLY cannot run inside a transaction block"
**Solução**: A migration já está configurada para usar RunPython que evita isso. Se ocorrer, é porque há outra migration em transação. Execute manualmente:
```sql
CREATE INDEX CONCURRENTLY idx_venda_flow_auditoria ON crm_venda(status_tratamento_id, ativo) WHERE status_tratamento_id IS NOT NULL AND status_esteira_id IS NULL AND ativo IS TRUE;
-- (repetir para outros índices)
```

### Erro: "relation already exists"
**Solução**: Índices já foram criados. Pode ignorar ou dropar e recriar.

### Performance não melhorou
**Verificar**:
1. Índices foram criados? `SELECT * FROM pg_indexes WHERE tablename='crm_venda';`
2. Estatísticas atualizadas? `ANALYZE crm_venda;`
3. PostgreSQL está usando os índices? `EXPLAIN ANALYZE SELECT ...`

---

## ✅ CONFIRMAÇÃO FINAL

Antes de executar em produção, confirme:

- [x] Código testado em desenvolvimento
- [x] Migrations aplicadas e testadas localmente
- [x] Script de validação executado com sucesso
- [x] Documentação revisada
- [ ] **BACKUP DO BANCO FEITO** ⚠️
- [ ] Equipe avisada sobre manutenção
- [ ] Plano de rollback preparado
- [ ] Horário adequado escolhido

---

## 🎯 COMANDO ÚNICO DE DEPLOY

Para facilitar, use o script automatizado:

**Windows/PowerShell**:
```powershell
.\scripts\deploy_performance.ps1
```

**Linux/Bash**:
```bash
bash scripts/deploy_performance.sh
```

---

## 📞 CONTATOS DE EMERGÊNCIA

Em caso de problemas críticos:
- DBA: [contato]
- DevOps: [contato]
- Responsável Técnico: [contato]

---

**Data**: 03/01/2026  
**Responsável**: [Seu Nome]  
**Status**: ✅ PRONTO PARA DEPLOY

---

## 🎉 PÓS-DEPLOY

Após deploy bem-sucedido:
1. ✅ Marcar task como concluída
2. 📧 Comunicar equipe sobre melhorias
3. 📊 Compartilhar métricas de antes/depois
4. 🎊 Celebrar! 🚀

**Sucesso!** As melhorias devem trazer ganhos significativos de performance!
