# Rekordbox Dedupe

**Trova i brani duplicati nella tua collezione rekordbox, tieni una sola copia e rimappa le playlist sulla copia tenuta. Interfaccia da terminale (TUI) per macOS.**

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

> **English (short):** a single-file, dependency-free Python tool with a terminal UI that finds duplicate tracks in a rekordbox library (via the XML export), lets you pick which copy to keep, and rewrites your playlists so they point to the kept copy. It never touches the rekordbox database or your audio files. The interface is in Italian.

---

## Il problema

Ogni volta che importi nuovi mp3 da analizzare e li metti in una playlist, può succedere che quella traccia **esista già in libreria** con un file diverso (altro rip, altro bitrate, altra cartella). Risultato: duplicati in collezione e nelle playlist.

Rekordbox Dedupe:

1. trova i duplicati,
2. sceglie (o ti fa scegliere) **una sola copia da tenere**,
3. **sostituisce nelle playlist** il duplicato con la copia tenuta, nella stessa posizione,
4. prepara una playlist con i duplicati da eliminare dalla collezione.

> ⚠️ Lo strumento lavora su un **XML esportato** da rekordbox. Non legge né scrive il database di rekordbox e non tocca i file audio: produce un nuovo XML e un report CSV che reimporti tu.

## Requisiti

- macOS (funziona su qualsiasi sistema con Python e `curses`, ma è pensato e testato per macOS)
- Python 3.8 o superiore (`python3 --version`)
- Nessuna libreria da installare: usa solo la libreria standard

## Installazione

```bash
git clone https://github.com/RickOrsi/rekordbox-library-dedupe-py.git
cd rekordbox-library-dedupe-py
```

Oppure scarica solo `rekordbox_dedupe_TUI.py`: è un singolo file.

## Uso rapido

1. In rekordbox: **File → Export Collection in xml format** (es. sul Desktop).
2. Nel Terminale:

   ```bash
   python3 rekordbox_dedupe_TUI.py
   ```

   oppure con il file già caricato:

   ```bash
   python3 rekordbox_dedupe_TUI.py --ui ~/Desktop/rekordbox.xml
   ```

3. Nella schermata iniziale scegli il file XML (puoi trascinarlo nel Terminale e incollare il percorso), imposta le opzioni e premi `a` per analizzare.
4. Controlla i gruppi di duplicati, correggi a mano i casi dubbi, poi premi `e` per esportare.

Vengono creati accanto all'XML originale:

| File | Contenuto |
|---|---|
| `<nome>_dedup.xml` | XML con le playlist rimappate + la playlist `_DUPLICATI DA ELIMINARE` |
| `<nome>_dedup_report.csv` | Report di ogni gruppo: cosa si tiene, cosa si rimuove, perché, in quali playlist stava |

## L'interfaccia

```
 Rekordbox Dedupe · rekordbox.xml
 127 gruppi · 127 copie da rimuovere · 0 ignorati · criterio: quality
    1  ✔  Artista – Titolo                                          (2)
    2  ✎  Altro Artista – Altro Titolo                              (3)
 ───────────────────────────────────────────────────────────────────────
 > TIENI     MP3    320k  5:01  cue:0   ok  2026-10-18
     /Users/me/Music/new/Artista - Titolo.mp3
   RIMUOVI   MP3    256k  5:00  cue:1   ok  2024-01-01
     /Users/me/Music/old/titolo.mp3
 Playlist della copia selezionata: House/Warmup | Sabato
```
*(esempio: l'aspetto reale dipende dal tuo terminale)*

Stato di ogni gruppo: `✔` scelta automatica · `✎` scelta manuale · `—` ignorato.

### Tasti

| Tasto | Azione |
|---|---|
| `↑` `↓` / `PgUp` `PgDn` / `Home` `End` | cambia gruppo |
| `←` `→` | seleziona la copia nel gruppo |
| `Spazio` / `Invio` | **tieni questa copia** (le altre vengono rimosse) |
| `i` | ignora il gruppo (non è un duplicato, non si tocca) |
| `r` / `R` | ripristina la scelta automatica (gruppo / tutti) |
| `p` | anteprima delle playlist che verranno modificate |
| `e` | esporta XML + report CSV |
| `s` | torna alle opzioni e rianalizza |
| `?` | aiuto |
| `q` | esci (le scelte non esportate si perdono) |

### Opzioni (schermata iniziale)

- **Copia da tenere**: `quality` (lossless > bitrate > cue), `cues` (privilegia cue/rating/play già fatti), `oldest`, `newest`
- **Confronto titoli**: *stretto* (Extended/Remix restano distinti) o *largo* (ignora le parentesi: trova di più, ma può unire remix diversi)
- **Confronto durata** e **tolleranza** (default 5 secondi)
- **Playlist esportate**: solo quelle modificate (default) o tutte
- **Suffisso playlist**: default ` [dedup]`

## Come riconosce i duplicati

Due brani sono lo stesso brano se, dopo la normalizzazione, hanno lo stesso **artista** e lo stesso **titolo** e una **durata** entro la tolleranza.

La normalizzazione ignora maiuscole, accenti e punteggiatura; tratta `&`, `feat`, `ft`, `vs`, virgole allo stesso modo e ignora l'ordine degli artisti; toglie i numeri di traccia iniziali e testo tipo "(Free Download)"; considera "Original Mix" come assente. Se mancano i tag usa il nome del file (`Artista - Titolo.mp3`).

Versioni diverse (Extended, Radio Edit, Remix) restano brani distinti.

## Come applicare il risultato in rekordbox

1. **Fai un backup**: File → Library → Backup Library.
2. Preferenze → Avanzate → Database → *rekordbox xml* → *Imported Library* → scegli `<nome>_dedup.xml`, poi **riavvia rekordbox**. (Verifica che in Vista → Layout sia spuntato "rekordbox xml". I nomi dei menu possono variare tra rekordbox 6 e 7.)
3. Nell'albero *rekordbox xml* fai clic destro su una playlist `[dedup]` → **Import Playlist**. Prova prima con una sola e controlla il risultato; poi importa le altre.
4. Importa anche `_DUPLICATI DA ELIMINARE`, aprila, seleziona tutto e **Rimuovi dalla collezione**.
5. Quando sei sicuro, cancella le vecchie playlist e togli il suffisso ` [dedup]` rinominando le nuove.

> Importa le playlist **prima** di eliminare i duplicati: se li elimini prima, rekordbox li toglie anche dalle playlist originali senza metterci la copia tenuta.
>
> Rekordbox non accetta playlist o cartelle con lo stesso nome nello stesso livello: per questo il suffisso ` [dedup]` è attivo di default.

## Modalità automatica (senza interfaccia)

```bash
python3 rekordbox_dedupe.py ~/Desktop/rekordbox.xml
```

Applica le scelte automatiche e scrive i file di output. Opzioni:

| Opzione | Descrizione |
|---|---|
| `--keep {quality,cues,oldest,newest}` | criterio per la copia da tenere (default `quality`) |
| `--loose` | ignora le parentesi nel titolo |
| `--ignore-duration` | non confrontare la durata |
| `--tolerance N` | tolleranza di durata in secondi (default 5) |
| `--all-playlists` | esporta tutte le playlist, non solo quelle modificate |
| `--suffix "TESTO"` | suffisso delle playlist modificate (`""` per nessuno) |
| `-o FILE`, `--report FILE` | percorsi di output |
| `--ui` | forza la TUI |

## Limiti noti e sicurezza

- Lavora sull'XML: **non modifica il database di rekordbox** né i file audio. Prima di importare, fai sempre un backup della libreria.
- Il comportamento dell'import da XML dipende da rekordbox: prova con **una sola playlist** prima di importare tutto.
- Nessun algoritmo di riconoscimento è perfetto: **controlla il report CSV** (righe `RIMUOVI`) prima di eliminare brani. Con `--loose` il rischio di unire versioni diverse aumenta.
- Se tieni la copia con bitrate più alto, le cue della copia scartata non vengono trasferite: usa `--keep cues` se il lavoro fatto sulle tracce conta più della qualità.
- Gli XML esportati contengono i percorsi dei tuoi file: **non pubblicarli** (il `.gitignore` di questo repo li esclude).


## Licenza

[MIT]

---

*Rekordbox è un marchio di AlphaTheta Corporation / Pioneer DJ. Questo progetto è indipendente e non è affiliato né approvato da AlphaTheta o Pioneer DJ.*
