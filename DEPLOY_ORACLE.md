# Migração 24/7 Grátis para Oracle Cloud Always Free — FyNex Quant AI

Guia rápido para rodar o **Dashboard + IA Ultra-Segura (`1h`)** 24 horas por dia, 7 dias por semana, com **disco permanente** (nunca perde o aprendizado), **muita RAM** e **zero bloqueio da Binance**.

---

## Passo 1 — Criar conta gratuita na Oracle Cloud
1. Acesse [cloud.oracle.com](https://www.oracle.com/br/cloud/free/) e crie sua conta **Always Free**.
2. **Home Region (muito importante):** escolha **Brazil East (Sao Paulo)** ou **Brazil Southeast (Vinhedo)** *(ou **Germany Central - Frankfurt**)*. Evite regiões dos EUA.
3. *(A Oracle solicita um cartão apenas para validação de identidade — não cobra nada no plano Always Free.)*

---

## Passo 2 — Criar a VM Ubuntu Gratuita (Always Free)
1. No painel da Oracle, vá em **Compute → Instances → Create Instance**.
2. **Name:** `fynex-quant-ai`
3. **Image and Shape:**
   - **Image:** `Canonical Ubuntu 24.04` (ou `22.04`)
   - **Shape:** clique em *Change Shape* → **Ampere (ARM)** → `VM.Standard.A1.Flex` *(Always Free)* → selecione **2 OCPUs e 12 GB de RAM** *(ou até 4 OCPUs / 24 GB)*.
   - *(Se não houver capacidade ARM na hora, escolha `VM.Standard.E2.1.Micro` — também Always Free, 1 GB RAM.)*
4. **Add SSH Keys:** clique em **Save Private Key** (guarde o arquivo `.key` no seu PC — você vai usar para entrar na VM).
5. Clique em **Create** e anote o **Public IP Address** que aparecer na tela.

---

## Passo 3 — Liberar a porta `8000` na rede da Oracle (VCN)
1. Na página da instância criada, clique no link da **Subnet** (em *Primary VNIC*).
2. Clique em **Default Security List**.
3. Clique em **Add Ingress Rules** e preencha:
   - **Source CIDR:** `0.0.0.0/0`
   - **IP Protocol:** `TCP`
   - **Destination Port Range:** `8000`
   - **Description:** `FyNex Dashboard`
4. Clique em **Add Ingress Rules**.

---

## Passo 4 — Conectar na VM e instalar em 1 comando
No PowerShell do seu Windows (na pasta onde salvou o arquivo `.key`):
```powershell
ssh -i sua-chave.key ubuntu@SEU_IP_PUBLICO
```

Já dentro do terminal da VM Ubuntu, execute:
```bash
git clone https://github.com/fynexconsultoria-droid/FyNex-Quant-AI.git
cd FyNex-Quant-AI
chmod +x setup_oracle.sh
./setup_oracle.sh
```

O script vai automaticamente:
- Instalar Docker e Docker Compose
- Abrir a porta `8000` no firewall do Ubuntu
- Pedir seu usuário e senha para proteger o dashboard
- Subir o container 24/7 com **volume persistente** (`trading_data.db`, `models/`, `logs/`)

---

## Passo 5 — Acessar de qualquer lugar (PC ou Celular)
Abra no navegador:
```
http://SEU_IP_PUBLICO:8000
```

Pronto! Mesmo com seu PC desligado, a IA `1h` continuará operando, salvando cada aprendizado no disco permanente e acompanhando notícias/macro 24/7.
