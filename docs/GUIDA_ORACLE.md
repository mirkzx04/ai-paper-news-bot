# Guida completa (da zero) — hostare il bot su Oracle Cloud Always Free

Guida passo-passo per mettere il bot su una VM Linux gratuita di Oracle, sempre
accesa, dove gira 24/7 in tempo reale. **Non serve sapere nulla di server**: ogni
comando è spiegato. Tempo: ~45 minuti (di cui parecchi di attesa).

> Convenzioni: i blocchi `così` sono comandi da incollare in un terminale.
> 💻 = sul **tuo PC** (Ubuntu). ☁️ = **dentro la VM** Oracle (dopo l'SSH).
> ⚠️ = punto dove ci si blocca spesso. Quando un passo non torna, **incollami
> l'output** e ti sblocco.

---

## Cosa ti serve prima di iniziare
- Una **carta di credito**: Oracle la chiede solo per verificare che non sei un
  bot; sul piano **Always Free** non addebita nulla (puoi scegliere account
  "Always Free" senza upgrade a pagamento).
- I tuoi **token Telegram**: `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. Li hai già
  nel file `.env` locale del progetto — tienili a portata.
- Il tuo PC con Ubuntu (quello che stai usando).

---

## Parte 1 — Crea l'account Oracle Cloud (gratis)

1. Vai su **https://www.oracle.com/cloud/free/** → **Start for free**.
2. Inserisci email, scegli **Italy** come paese, verifica l'email.
3. Compila i dati e aggiungi la carta (verifica, nessun addebito sul Free Tier).
4. ⚠️ **Home Region**: ti viene chiesta una *region* (un data center). È
   **permanente**. Scegline una europea vicina (es. *Germany Central (Frankfurt)*
   o *Italy Northwest (Milan)* se c'è). Nota: la disponibilità di VM ARM gratuite
   varia per region — se più avanti vai sempre "out of capacity", il problema è
   quasi sempre la region troppo affollata.
5. Finisci la registrazione e fai login alla **console**: https://cloud.oracle.com

---

## Parte 2 — Crea la chiave SSH (sul tuo PC) 💻

SSH è il modo sicuro di collegarti alla VM. Funziona con due "chiavi": una
**privata** che resta sul tuo PC e una **pubblica** che metti sulla VM. Genera la
coppia (sul **tuo** terminale Ubuntu):

```
ssh-keygen -t ed25519 -f ~/.ssh/oracle_bot -C "oracle-bot"
```
- Quando chiede la *passphrase*, premi **Invio** due volte (nessuna password) —
  va benissimo per iniziare.
- Crea due file: `~/.ssh/oracle_bot` (privata, **non condividerla mai**) e
  `~/.ssh/oracle_bot.pub` (pubblica).

Mostra la **pubblica** (ti servirà tra un attimo da incollare su Oracle):
```
cat ~/.ssh/oracle_bot.pub
```
Copia tutta la riga che stampa (inizia con `ssh-ed25519 ...`).

---

## Parte 3 — Crea la VM (console Oracle)

1. Nella console: menu in alto a sinistra **☰** → **Compute** → **Instances** →
   **Create instance**.
2. **Name**: `paper-news-bot`.
3. **Image and shape** → **Edit**:
   - **Image**: clicca *Change image* → **Canonical Ubuntu** → **24.04**.
   - **Shape**: clicca *Change shape* → tab **Ampere** → **VM.Standard.A1.Flex** →
     imposta **1 OCPU** e **6 GB** di RAM (tutto dentro l'Always Free). Conferma.
   - ⚠️ **"Out of host capacity"**: capita spessissimo con le ARM gratis. Cosa
     fare, in ordine: (a) **riprova** dopo qualche minuto/ora; (b) nel riquadro
     *Availability domain* prova **AD-1 / AD-2 / AD-3**; (c) se proprio non va,
     scegli shape **VM.Standard.E2.1.Micro** (x86, 1 GB — più lento, ma si crea
     sempre; più avanti aggiungiamo un po' di "swap"). Tutto il resto della guida
     è identico.
4. **Networking**: lascia i default (crea una nuova *VCN*). Verifica che
   **Assign a public IPv4 address** sia **Yes**.
5. **Add SSH keys**: seleziona **Paste public keys** e incolla la riga di
   `oracle_bot.pub` (Parte 2).
6. **Create**. Aspetta ~1 minuto che lo stato diventi **Running**.
7. Nella pagina dell'istanza, copia il **Public IP address** (lo chiamo `<IP>`).

---

## Parte 4 — Verifica il firewall (solo SSH)

Il bot si collega *in uscita* a Telegram (non riceve connessioni), quindi serve
aperta **solo** la porta SSH (22), che di default lo è già. Per controllare:
**Instance → Virtual Cloud Network → Security Lists → Default Security List →
Ingress Rules**: deve esserci una regola `22/tcp` da `0.0.0.0/0`. **Non aprire
altro** (niente 80/443).

---

## Parte 5 — Collegati alla VM 💻 → ☁️

Sul **tuo** terminale:
```
ssh -i ~/.ssh/oracle_bot ubuntu@<IP>
```
(sostituisci `<IP>`). Alla prima volta scrivi `yes`. Se vedi un prompt tipo
`ubuntu@paper-news-bot:~$` **sei dentro la VM**. 🎉

⚠️ Se dà *Connection refused/timeout*: la VM potrebbe non essere ancora pronta
(attendi 1-2 min) o il firewall non ha la regola SSH (Parte 4). *Permission
denied (publickey)*: hai incollato la chiave **pubblica** giusta nella Parte 3?

---

## Parte 6 — Prepara la VM ☁️

D'ora in poi i comandi sono **dentro la VM**. Aggiorna il sistema e installa il
necessario:
```
sudo apt update && sudo apt -y upgrade
sudo apt install -y python3.12 python3.12-venv git
```
Crea un utente dedicato (buona pratica: il bot non gira come amministratore):
```
sudo useradd --system --create-home --shell /usr/sbin/nologin paperbot
```

### (Solo se hai la micro da 1 GB) aggiungi swap
Serve a non esaurire la RAM quando scarica il modello SPECTER:
```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```
(La VM ARM da 6 GB non ne ha bisogno — salta questo blocco.)

---

## Parte 7 — Scarica il bot e installa le dipendenze ☁️

Crea la cartella e diventa l'utente `paperbot`:
```
sudo install -d -o paperbot -g paperbot /opt/paper-news-bot
sudo -u paperbot -H bash
cd /opt/paper-news-bot
```
Scarica il codice e crea l'ambiente Python:
```
git clone https://github.com/mirkzx04/ai-paper-news-bot.git .
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```
⏳ L'install di `torch` + dipendenze richiede qualche minuto. (Usiamo la versione
**CPU** di torch perché la VM non ha GPU.)

---

## Parte 8 — Inserisci i tuoi token ☁️

```
cp .env.example .env
nano .env
```
Nell'editor `nano`: imposta `TELEGRAM_BOT_TOKEN=...` e `TELEGRAM_CHAT_ID=...`
(gli stessi del tuo `.env` locale). **Ignora le righe `GIST_*`**: servono solo
all'altra modalità (GitHub Actions); qui lo stato è locale.
Salva con **Ctrl+O**, Invio, esci con **Ctrl+X**.

---

## Parte 9 — Prova a mano ☁️

Con l'ambiente ancora attivo (`source .venv/bin/activate` se l'avevi chiuso):
```
python main.py --register-menu
```
→ Su Telegram, nella chat col bot, tocca il pulsante **/** o **☰**: dovresti
vedere la lista dei comandi.
```
python main.py --serve -v
```
→ Parte il processo. **Manda `/start` o vota un paper su Telegram**: vedrai
attività nei log e — novità della VM — l'ack **in tempo reale**. Ferma con
**Ctrl+C** prima di passare al servizio automatico.

---

## Parte 10 — Rendilo un servizio sempre attivo (systemd) ☁️

`systemd` è il "gestore di servizi" di Linux: tiene il bot acceso, lo riavvia se
crasha e lo fa ripartire al reboot. Esci dall'utente paperbot per usare `sudo`:
```
exit
```
Installa il servizio (il file è già nel repo):
```
sudo cp /opt/paper-news-bot/deploy/bot.service /etc/systemd/system/paper-news-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now paper-news-bot
```
Controlla che sia attivo:
```
systemctl status paper-news-bot
```
Deve dire **active (running)**. (`q` per uscire dalla vista.)

> I path nel file (`/opt/paper-news-bot`, utente `paperbot`) coincidono con questa
> guida, quindi di norma non serve modificarli. Se hai usato path diversi:
> `sudo nano /etc/systemd/system/paper-news-bot.service` e adatta le righe
> `User=`, `WorkingDirectory=`, `ExecStart=`, `EnvironmentFile=`.

---

## Parte 11 — Guarda i log e verifica

```
journalctl -u paper-news-bot -f
```
(log dal vivo; **Ctrl+C** per uscire dalla vista, il bot resta acceso). Manda
qualche comando al bot da Telegram e osserva. Per la verifica completa
(voti 👍/👎, toggle, heartbeat) segui `docs/DRY_RUN.md`.

---

## Operatività quotidiana

**Aggiornare il bot** quando cambi il codice:
```
sudo -u paperbot -H bash -c 'cd /opt/paper-news-bot && git pull'
sudo systemctl restart paper-news-bot
```
(Se è cambiato `requirements.txt`, prima reinstalla:
`sudo -u paperbot /opt/paper-news-bot/.venv/bin/pip install -r /opt/paper-news-bot/requirements.txt`.)

**Stop / start / restart**:
```
sudo systemctl stop paper-news-bot
sudo systemctl start paper-news-bot
sudo systemctl restart paper-news-bot
```

**Backup dello stato** (voti, storico). Su VM lo stato è solo locale in `data/`:
se perdi la VM, perdi tutto. Un backup notturno (come utente paperbot,
`crontab -e`):
```cron
30 3 * * * tar -czf /opt/paper-news-bot/backups/data-$(date +\%F).tgz -C /opt/paper-news-bot data
35 3 * * * find /opt/paper-news-bot/backups -name 'data-*.tgz' -mtime +14 -delete
```
(crea prima `sudo install -d -o paperbot -g paperbot /opt/paper-news-bot/backups`).

---

## Problemi comuni

| Sintomo | Causa / soluzione |
| --- | --- |
| `Out of host capacity` alla creazione | ARM gratis esaurite: riprova / cambia AD / usa la micro x86 (Parte 3) |
| SSH `Connection timed out` | VM non ancora *Running*, o manca la regola SSH nel firewall (Parte 4) |
| SSH `Permission denied (publickey)` | chiave pubblica sbagliata/non incollata (Parti 2-3); usa `-i ~/.ssh/oracle_bot` |
| Il bot non risponde su Telegram | token/chat_id errati nel `.env`; controlla `journalctl -u paper-news-bot -p warning` |
| Processo killed / OOM sulla micro | manca lo swap (Parte 6) |
| `git clone` fallisce | controlla l'URL `https://github.com/mirkzx04/ai-paper-news-bot.git` |

Quando ti blocchi, **copiami l'errore esatto** e ti dico cosa fare.
