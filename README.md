# NeuroStudi – MVP (senza costi di LLM)

App per pazienti neurologici e familiari: scelgono diagnosi (5 macrogruppi) ed età, vedono gli studi aperti in Lombardia, Toscana e Lazio e chiamano direttamente il gruppo di ricerca. Nessun dato personale raccolto, nessun account, nessuna API a pagamento.

## File

| File | A cosa serve |
|---|---|
| `app.html` | L’app, un unico file statico ospitabile ovunque. Contiene dati dimostrativi finché non la aggiorni. |
| `pipeline.py` | Scarica i registri, filtra, scrive i testi in italiano, seleziona i recapiti diretti, produce `trials.json` e l’app con i dati reali. |
| `centri_partner.csv` | Modello per i contatti confermati dai centri che aderiscono. |
| `centri_senza_contatto.csv` | Prodotto dalla pipeline: centri attivi senza recapito diretto, con il nome del responsabile quando c’è. È la lista da contattare per proporre l’adesione. |

## Aggiornare i dati

```bash
pip install requests
python pipeline.py --partner centri_partner.csv --html app.html
# produce trials.json, centri_senza_contatto.csv e app_aggiornata.html
```

Con GitHub l’aggiornamento è automatico ogni notte (vedi `aggiorna.yml`). Opzioni: `--no-ctis` (solo ClinicalTrials.gov), `-v` (log dettagliato, utile per vedere gli studi e i recapiti scartati).

## Testi in italiano: due modalità gratuite

**Predefinita, senza LLM.** Titolo e sintesi sono costruiti con regole fisse dai campi strutturati del registro: tipo di intervento, fase, randomizzazione, placebo, cecità, numero di partecipanti. Sono testi semplici ma sempre corretti, perché non c’è nulla di inventato. La descrizione originale in inglese resta consultabile nell’app.

**Facoltativa, LLM gratuito in locale con [Ollama](https://ollama.com).** Testi più scorrevoli, inclusa la sezione «Cosa comporta partecipare». Gira sul tuo computer o server, senza costi né invio di dati a terzi (al modello passano solo testi pubblici dei registri).

```bash
ollama pull qwen2.5:7b          # circa 5 GB, serve una macchina con almeno 8 GB di RAM
python pipeline.py --llm ollama --partner centri_partner.csv --html app.html
```

Placebo, età, fase e macrogruppo restano sempre calcolati dai dati strutturati, mai dall’LLM. I testi generati che contengono promesse («guarisce», «garantito») o hanno lunghezze anomale vengono scartati e sostituiti da quelli a regole. Le risposte sono salvate in `.cache_llm.json`, quindi ogni studio viene elaborato una sola volta.

## Recapiti: solo il gruppo di ricerca

La pipeline non usa mai i contatti centrali dello studio (di solito sponsor o CRO). Per ogni centro esamina i contatti del sito e scarta:

- numeri esteri, numeri verdi (800, 803) e a pagamento;
- nomi generici o aziendali («Site Public Contact», call center, URP, centralino, società farmaceutiche);
- email generiche (info@, urp@, centralino@, PEC) e, per gli studi sponsorizzati dall’industria, email del dominio dello sponsor;
- telefoni o email uguali a quelli centrali dello studio, o usati da tre o più centri diversi (linee centralizzate).

Tra i contatti rimasti preferisce chi ha un telefono e, a parità, il responsabile dello studio nel centro. Se non resta nulla, l’app mostra il nome del responsabile (se pubblicato) e invita a passare dal proprio neurologo; il centro finisce in `centri_senza_contatto.csv`.

I contatti di `centri_partner.csv` hanno sempre la precedenza e compaiono come «Contatto confermato dal centro».

## Limiti noti

- **CTIS**: l’API pubblica non ha documentazione ufficiale stabile; l’adattatore è tollerante e, se fallisce, la pipeline continua con ClinicalTrials.gov. CTIS in genere non pubblica recapiti dei centri: aggiunge siti, i recapiti arrivano da ClinicalTrials.gov o dai partner.
- **Euristiche sui recapiti**: riducono molto i centralini, ma non possono garantirlo al 100%. Controlla a campione i primi risultati con `-v`.
- **Città**: se un centro di una città minore non viene riconosciuto, aggiungila a `REGIONI` in `pipeline.py`.

## Privacy

L’app non salva diagnosi né età e non invia nulla a server esterni: anche il riconoscimento della diagnosi scritta a parole avviene sul dispositivo. Prima del lancio pubblico fai comunque verificare informativa e destinazione d’uso da un consulente (GDPR, MDR).
