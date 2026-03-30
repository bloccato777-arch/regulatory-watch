# Regulatory Watch

Aggregatore automatico di pubblicazioni dalle autorità di vigilanza bancaria europee e normative EU (DORA, NIS2, AI Act).

## Fonti monitorate

| Fonte | Tipo | Frequenza |
|-------|------|-----------|
| Banca d'Italia (comunicati, notizie) | RSS + Scraping | Ogni 30 min |
| ECB / SSM (press, speeches, publications) | RSS | Ogni 30 min |
| EBA (news, press releases) | RSS + Scraping | Ogni 30 min |
| ESMA (news, press releases) | Scraping | Ogni 30 min |
| DORA (digital-operational-resilience-act.com) | Scraping | Ogni 30 min |
| NIS2 (nis-2-directive.com) | Scraping | Ogni 30 min |
| AI Act (EC digital-strategy) | Scraping | Ogni 30 min |
| EUR-Lex (legislative updates) | RSS | Ogni 30 min |

## Avvio rapido (locale)

```bash
# Installa dipendenze
pip install -r requirements.txt

# Avvia l'app
python app.py

# Apri http://localhost:5000
```

## Deploy su Render (consigliato, gratuito)

1. Crea un repository GitHub con questi file
2. Vai su [render.com](https://render.com) → New → Web Service
3. Collega il repository
4. Render rileverà automaticamente `render.yaml`
5. Click **Deploy**

L'app sarà disponibile all'URL `https://regulatory-watch-xxxx.onrender.com`

## Deploy su Railway

```bash
# Installa Railway CLI
npm install -g @railway/cli

# Login e deploy
railway login
railway init
railway up
```

## Deploy con Docker

```bash
docker build -t regulatory-watch .
docker run -p 5000:5000 regulatory-watch
```

## API

| Endpoint | Metodo | Descrizione |
|----------|--------|-------------|
| `/` | GET | Frontend web |
| `/api/news` | GET | Tutte le news (param: `?tag=eba`) |
| `/api/refresh` | POST | Forza aggiornamento |
| `/api/status` | GET | Stato del sistema |

## Configurazione

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `PORT` | 5000 | Porta del server |
| `FETCH_INTERVAL` | 30 | Minuti tra un aggiornamento e l'altro |
