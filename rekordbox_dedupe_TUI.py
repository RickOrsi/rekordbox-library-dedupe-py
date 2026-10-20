#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rekordbox_dedupe.py  -  deduplica una collezione rekordbox e rimappa le playlist
--------------------------------------------------------------------------------
Legge un XML esportato da rekordbox (File > Export Collection in xml format),
trova i brani duplicati, sceglie UNA copia da tenere e riscrive le playlist in
modo che puntino solo a quella. NON tocca mai il database di rekordbox ne' i
file audio: produce un nuovo XML + un report CSV che reimporti tu.

Solo libreria standard di Python 3.8+ (su macOS: python3).

    python3 rekordbox_dedupe.py                 # interfaccia da terminale (TUI)
    python3 rekordbox_dedupe.py ~/Desktop/rekordbox.xml     # modalita' automatica (CLI)
    python3 rekordbox_dedupe.py --ui ~/Desktop/rekordbox.xml  # TUI con file precaricato
"""

import argparse
import copy
import csv
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import unquote, urlparse

LOSSLESS_EXT = {"flac", "wav", "aiff", "aif", "alac"}
ARTIST_STOP = {"and", "feat", "ft", "featuring", "vs", "with", "x"}
JUNK_PATTERNS = [
    r"\[?\(?\s*free download\s*\)?\]?",
    r"\(?\[?\s*official (video|audio|music video|lyric video)\s*\)?\]?",
    r"(www\.)?[a-z0-9\-]+\.(com|net|org|ru|info|biz)\b",
    r"\b(320 ?kbps|256 ?kbps|128 ?kbps)\b",
]
KEEP_MODES = ["quality", "cues", "oldest", "newest"]
KEEP_DESC = {
    "quality": "qualita' (lossless > bitrate > cue)",
    "cues": "lavoro fatto (cue/rating/play > qualita')",
    "oldest": "il piu' vecchio in libreria",
    "newest": "il piu' recente",
}


# ==========================================================================
#  CORE
# ==========================================================================
class Options:
    def __init__(self, keep="quality", loose=False, ignore_duration=False,
                 tolerance=5.0, suffix=" [dedup]", all_playlists=False):
        self.keep = keep
        self.loose = loose
        self.ignore_duration = ignore_duration
        self.tolerance = tolerance
        self.suffix = suffix
        self.all_playlists = all_playlists


class Group:
    def __init__(self, ranked, reason):
        self.items = ranked              # migliore per primo
        self.reason = reason
        self.auto_keep = ranked[0].get("TrackID")


class Analysis:
    pass


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def base_clean(s):
    s = strip_accents(s or "").lower()
    s = s.replace("&", " and ")
    for p in JUNK_PATTERNS:
        s = re.sub(p, " ", s)
    return s


def norm_artist(s):
    s = re.sub(r"[^a-z0-9]+", " ", base_clean(s))
    return " ".join(sorted(t for t in s.split() if t not in ARTIST_STOP))


def norm_title(s, loose=False):
    s = base_clean(s)
    s = re.sub(r"^\s*\d{1,3}\s*[\.\-_]\s*", "", s)
    s = re.sub(r"[\(\[]\s*original (mix|version)\s*[\)\]]", " ", s)
    s = re.sub(r"\boriginal (mix|version)\b$", " ", s)
    if loose:
        s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def location_to_path(loc):
    return unquote(urlparse(loc).path) if loc else ""


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def date_int(t, missing):
    d = (t.get("DateAdded") or "").replace("-", "")
    return int(d) if d.isdigit() else missing


def track_names(t):
    artist = (t.get("Artist") or "").strip()
    title = (t.get("Name") or "").strip()
    if not title or not artist:
        stem = os.path.splitext(os.path.basename(location_to_path(t.get("Location"))))[0]
        if " - " in stem:
            a, ti = stem.split(" - ", 1)
            artist, title = artist or a, title or ti
        else:
            title = title or stem
    return artist, title


def is_lossless(t):
    ext = os.path.splitext(location_to_path(t.get("Location")))[1].lstrip(".").lower()
    kind = (t.get("Kind") or "").lower()
    return ext in LOSSLESS_EXT or any(k in kind for k in LOSSLESS_EXT)


def richness(t):
    return (len(t.findall("POSITION_MARK")),
            1 if t.find("TEMPO") is not None else 0,
            to_int(t.get("Rating")), to_int(t.get("PlayCount")))


def score(t, mode, exists, in_playlists):
    lossless = 1 if is_lossless(t) else 0
    bitrate = to_int(t.get("BitRate"))
    rich = richness(t)
    tid = -to_int(t.get("TrackID"))
    if mode == "quality":
        return (exists, lossless, bitrate, rich, in_playlists, -date_int(t, 99999999), tid)
    if mode == "cues":
        return (exists, rich, lossless, bitrate, in_playlists, -date_int(t, 99999999), tid)
    if mode == "oldest":
        return (exists, -date_int(t, 99999999), lossless, bitrate, rich, tid)
    return (exists, date_int(t, 0), lossless, bitrate, rich, tid)


def why(winner, others, exists_map):
    r = []
    if exists_map[winner.get("TrackID")] and not all(exists_map[o.get("TrackID")] for o in others):
        r.append("il file dell'altra copia manca su disco")
    if is_lossless(winner) and not all(is_lossless(o) for o in others):
        r.append("lossless")
    wb = to_int(winner.get("BitRate"))
    if all(wb > to_int(o.get("BitRate")) for o in others):
        r.append("bitrate piu' alto")
    if all(richness(winner) > richness(o) for o in others):
        r.append("piu' cue/beatgrid/rating/play")
    return ", ".join(r) or "parita': scelta per data/ID"


def walk_playlists(node, path=()):
    for child in node.findall("NODE"):
        name = child.get("Name", "")
        if child.get("Type") == "1":
            yield path + (name,), child
        else:
            yield from walk_playlists(child, path + (name,))


def prune(node, only_changed, changed):
    for child in list(node.findall("NODE")):
        if child.get("Type") == "1":
            if only_changed and child not in changed:
                node.remove(child)
        elif not prune(child, only_changed, changed):
            node.remove(child)
    if node.get("Type") == "0":
        node.set("Count", str(len(node.findall("NODE"))))
    return len(node.findall("NODE")) > 0 or node.get("Name") == "ROOT"


def load_xml(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.find("COLLECTION") is None or root.find("PLAYLISTS") is None:
        raise ValueError("Questo non sembra un XML di rekordbox (manca COLLECTION o PLAYLISTS).")
    return tree


def analyze(tree, opts):
    root = tree.getroot()
    tracks = {t.get("TrackID"): t for t in root.find("COLLECTION").findall("TRACK")}
    loc_to_id = {t.get("Location"): tid for tid, t in tracks.items() if t.get("Location")}
    root_node = root.find("PLAYLISTS").find("NODE")

    membership = defaultdict(list)
    for path, pl in walk_playlists(root_node):
        kt = pl.get("KeyType", "0")
        for tr in pl.findall("TRACK"):
            tid = tr.get("Key") if kt == "0" else loc_to_id.get(tr.get("Key"))
            if tid:
                membership[tid].append("/".join(path))

    exists_map = {tid: os.path.exists(location_to_path(t.get("Location"))) for tid, t in tracks.items()}

    buckets = defaultdict(list)
    for t in tracks.values():
        a, ti = track_names(t)
        k = (norm_artist(a), norm_title(ti, opts.loose))
        if k[1]:
            buckets[k].append(t)

    clusters = []
    for items in buckets.values():
        if len(items) < 2:
            continue
        if opts.ignore_duration:
            clusters.append(items)
            continue
        items = sorted(items, key=lambda t: to_int(t.get("TotalTime")))
        cl = [items[0]]
        for t in items[1:]:
            if abs(to_int(t.get("TotalTime")) - to_int(cl[0].get("TotalTime"))) <= opts.tolerance:
                cl.append(t)
            else:
                if len(cl) > 1:
                    clusters.append(cl)
                cl = [t]
        if len(cl) > 1:
            clusters.append(cl)

    groups = []
    for items in clusters:
        ranked = sorted(items, key=lambda t: score(t, opts.keep, exists_map[t.get("TrackID")],
                                                   len(membership[t.get("TrackID")])), reverse=True)
        groups.append(Group(ranked, why(ranked[0], ranked[1:], exists_map)))
    groups.sort(key=lambda g: tuple(s.lower() for s in track_names(g.items[0])))

    an = Analysis()
    an.tree, an.tracks, an.membership, an.exists, an.groups = tree, tracks, membership, exists_map, groups
    return an


def default_decisions(an):
    return {i: g.auto_keep for i, g in enumerate(an.groups)}


def affected_preview(an, decisions):
    """Playlist che verrebbero toccate: percorso -> numero di brani sostituiti."""
    out = defaultdict(int)
    for gi, g in enumerate(an.groups):
        keep = decisions.get(gi)
        if keep is None:
            continue
        for t in g.items:
            tid = t.get("TrackID")
            if tid != keep:
                for p in an.membership[tid]:
                    out[p] += 1
    return dict(out)


def apply_decisions(an, decisions, opts):
    tree = copy.deepcopy(an.tree)
    root_node = tree.getroot().find("PLAYLISTS").find("NODE")
    remap, removed_ids, rows = {}, [], []

    for gi, g in enumerate(an.groups):
        keep = decisions.get(gi)
        if keep is None:
            continue
        for t in g.items:
            tid = t.get("TrackID")
            is_keep = tid == keep
            if not is_keep:
                remap[tid] = keep
                removed_ids.append(tid)
            a, ti = track_names(t)
            rows.append({
                "gruppo": gi + 1, "azione": "TIENI" if is_keep else "RIMUOVI", "track_id": tid,
                "artista": a, "titolo": ti, "formato": t.get("Kind", ""), "bitrate": t.get("BitRate", ""),
                "durata_s": t.get("TotalTime", ""), "aggiunto": t.get("DateAdded", ""),
                "cue_loop": len(t.findall("POSITION_MARK")),
                "file_esiste": "si" if an.exists[tid] else "NO",
                "playlist": " | ".join(sorted(set(an.membership[tid]))),
                "motivo": (g.reason if keep == g.auto_keep else "scelta manuale") if is_keep else "",
                "percorso_file": location_to_path(t.get("Location")),
            })

    remap_loc = {an.tracks[r].get("Location"): an.tracks[k].get("Location") for r, k in remap.items()}
    changed, affected = set(), {}
    for path, pl in walk_playlists(root_node):
        mapping = remap if pl.get("KeyType", "0") == "0" else remap_loc
        seen, kept, replaced, inner = set(), [], 0, 0
        for tr in pl.findall("TRACK"):
            key = tr.get("Key")
            new_key = mapping.get(key, key)
            if new_key != key:
                replaced += 1
                tr.set("Key", new_key)
            if new_key in seen:
                inner += 1
                continue
            seen.add(new_key)
            kept.append(tr)
        if replaced or inner:
            for tr in pl.findall("TRACK"):
                pl.remove(tr)
            for tr in kept:
                pl.append(tr)
            pl.set("Entries", str(len(kept)))
            changed.add(pl)
            affected["/".join(path)] = (replaced, inner)
            if opts.suffix:
                pl.set("Name", pl.get("Name", "") + opts.suffix)

    if removed_ids:
        d = ET.SubElement(root_node, "NODE", {"Name": "_DUPLICATI DA ELIMINARE", "Type": "1",
                                              "KeyType": "0", "Entries": str(len(removed_ids))})
        for tid in removed_ids:
            ET.SubElement(d, "TRACK", {"Key": tid})
        changed.add(d)

    prune(root_node, not opts.all_playlists, changed)
    root_node.set("Count", str(len(root_node.findall("NODE"))))
    return {"tree": tree, "rows": rows, "removed": removed_ids, "affected": affected}


def default_outputs(src):
    stem = os.path.splitext(src)[0]
    return stem + "_dedup.xml", stem + "_dedup_report.csv"


def write_outputs(result, out_xml, out_csv):
    tree = result["tree"]
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass
    tree.write(out_xml, encoding="UTF-8", xml_declaration=True)
    cols = ["gruppo", "azione", "track_id", "artista", "titolo", "formato", "bitrate", "durata_s",
            "aggiunto", "cue_loop", "file_esiste", "playlist", "motivo", "percorso_file"]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        w.writerows(result["rows"])


# ==========================================================================
#  TUI (curses)
# ==========================================================================
def run_tui(initial_path=None):
    import curses
    import locale
    import subprocess
    import textwrap

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")

    state = {"colors": False}

    def col(n):
        return curses.color_pair(n) if state["colors"] else 0

    def put(win, y, x, text, attr=0):
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return
        try:
            win.addstr(y, x, str(text)[:max(0, w - x - 1)], attr)
        except curses.error:
            pass

    def hline(win, y):
        h, w = win.getmaxyx()
        put(win, y, 0, "─" * (w - 1), col(5))

    def header(win, title):
        h, w = win.getmaxyx()
        put(win, 0, 0, (" " + title).ljust(w - 1), curses.A_REVERSE | curses.A_BOLD)

    def too_small(win):
        h, w = win.getmaxyx()
        if h < 14 or w < 60:
            win.erase()
            put(win, 0, 0, "Finestra troppo piccola (min 60x14).")
            put(win, 1, 0, "Ingrandiscila oppure premi q.")
            win.refresh()
            return True
        return False

    def prompt(win, label, initial=""):
        buf = list(initial)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                h, w = win.getmaxyx()
                text = label + "".join(buf)
                shown = text[-(w - 2):]
                win.move(h - 2, 0)
                win.clrtoeol()
                put(win, h - 2, 0, shown, curses.A_BOLD)
                put(win, h - 1, 0, " Invio conferma · Esc annulla · Ctrl-U cancella tutto", col(5))
                win.move(h - 2, min(len(shown), w - 2))
                win.refresh()
                ch = win.get_wch()
                if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                    return "".join(buf)
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif ch == "\x15":
                    buf = []
                elif isinstance(ch, str) and ch.isprintable():
                    buf.append(ch)
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def popup(win, title, lines, footer=" ↑↓ scorri · Invio/q chiudi", keys=None):
        keys = keys or {}
        top = 0
        while True:
            win.erase()
            h, w = win.getmaxyx()
            header(win, title)
            body = max(1, h - 3)
            for i, ln in enumerate(lines[top:top + body]):
                put(win, 2 + i, 2, ln[:w - 4])
            put(win, h - 1, 0, footer, col(5))
            win.refresh()
            ch = win.getch()
            if ch in keys:
                return keys[ch]
            if ch in (curses.KEY_DOWN, ord("j")):
                top = min(max(0, len(lines) - body), top + 1)
            elif ch in (curses.KEY_UP, ord("k")):
                top = max(0, top - 1)
            elif ch == curses.KEY_NPAGE:
                top = min(max(0, len(lines) - body), top + body)
            elif ch == curses.KEY_PPAGE:
                top = max(0, top - body)
            elif not keys and ch in (27, ord("q"), 10, 13, curses.KEY_ENTER):
                return None

    def clean_path(p):
        p = p.strip()
        if len(p) > 1 and p[0] == p[-1] and p[0] in "'\"":
            p = p[1:-1]
        p = re.sub(r"\\(.)", r"\1", p)          # "\ " dal drag&drop di Terminale
        return os.path.expanduser(p.strip())

    # ---- schermata impostazioni -------------------------------------------
    def screen_setup(win, path, opts, msg=""):
        sel = 0
        items = ["file", "keep", "loose", "nodur", "tol", "allpl", "suffix", "go"]
        while True:
            if too_small(win):
                if win.getch() in (ord("q"), 27):
                    return None
                continue
            win.erase()
            h, w = win.getmaxyx()
            header(win, "Rekordbox Dedupe · impostazioni")
            put(win, 2, 2, "Trova i duplicati nella collezione e rimappa le playlist.", col(5))
            put(win, 3, 2, "Lavora sull'XML esportato: il database di rekordbox non viene toccato.", col(5))
            vals = {
                "file": ("File XML", path or "(premi Invio e incolla / trascina qui il file .xml)"),
                "keep": ("Copia da tenere", KEEP_DESC[opts.keep]),
                "loose": ("Confronto titoli", "largo (ignora le parentesi)" if opts.loose else "stretto (Extended/Remix restano distinti)"),
                "nodur": ("Confronto durata", "ignorata" if opts.ignore_duration else "attiva"),
                "tol": ("Tolleranza durata", "%g secondi" % opts.tolerance),
                "allpl": ("Playlist esportate", "tutte" if opts.all_playlists else "solo quelle modificate"),
                "suffix": ("Suffisso playlist", repr(opts.suffix) if opts.suffix else "(nessuno)"),
                "go": ("", "▶  ANALIZZA"),
            }
            y = 5
            for i, k in enumerate(items):
                label, value = vals[k]
                attr = curses.A_REVERSE if i == sel else 0
                if k == "go":
                    y += 1
                    put(win, y, 2, " " + value + " ", attr | curses.A_BOLD | col(1))
                else:
                    put(win, y, 2, ("%-20s" % label), attr | curses.A_BOLD)
                    put(win, y, 23, value, attr)
                y += 1
            if msg:
                put(win, h - 3, 2, msg, col(2) | curses.A_BOLD)
            put(win, h - 1, 0, " ↑↓ scegli · ←→/Spazio cambia · Invio modifica · a analizza · q esci", col(5))
            win.refresh()
            ch = win.getch()
            k = items[sel]
            msg = ""
            if ch in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(items)
            elif ch in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(items)
            elif ch in (ord("q"), 27):
                return None
            elif ch == ord("a") or (k == "go" and ch in (10, 13, curses.KEY_ENTER, ord(" "))):
                if not path or not os.path.isfile(path):
                    msg = "Seleziona prima un file XML valido."
                    sel = 0
                    continue
                return path
            elif ch in (10, 13, curses.KEY_ENTER, ord(" "), curses.KEY_RIGHT, curses.KEY_LEFT):
                step = -1 if ch == curses.KEY_LEFT else 1
                if k == "file":
                    r = prompt(win, "File XML: ", path or "")
                    if r is not None:
                        path = clean_path(r)
                        if not os.path.isfile(path):
                            msg = "File non trovato: " + path
                elif k == "keep":
                    opts.keep = KEEP_MODES[(KEEP_MODES.index(opts.keep) + step) % len(KEEP_MODES)]
                elif k == "loose":
                    opts.loose = not opts.loose
                elif k == "nodur":
                    opts.ignore_duration = not opts.ignore_duration
                elif k == "allpl":
                    opts.all_playlists = not opts.all_playlists
                elif k == "tol":
                    r = prompt(win, "Tolleranza (secondi): ", "%g" % opts.tolerance)
                    try:
                        if r is not None:
                            opts.tolerance = max(0.0, float(r.replace(",", ".")))
                    except ValueError:
                        msg = "Numero non valido."
                elif k == "suffix":
                    r = prompt(win, "Suffisso playlist (vuoto = nessuno): ", opts.suffix)
                    if r is not None:
                        opts.suffix = r

    # ---- schermata gruppi ---------------------------------------------------
    def fmt_ext(t):
        e = os.path.splitext(location_to_path(t.get("Location")))[1].lstrip(".").upper()
        return e or (t.get("Kind") or "?")[:5]

    def mmss(t):
        s = to_int(t.get("TotalTime"))
        return "%d:%02d" % (s // 60, s % 60)

    def screen_groups(win, an, opts, src):
        decisions = default_decisions(an)
        gi, gtop, ci, msg = 0, 0, 0, ""
        n = len(an.groups)
        while True:
            if too_small(win):
                if win.getch() in (ord("q"), 27):
                    return "quit"
                continue
            win.erase()
            h, w = win.getmaxyx()
            to_remove = sum(len(g.items) - 1 for i, g in enumerate(an.groups) if decisions[i] is not None)
            skipped = sum(1 for v in decisions.values() if v is None)
            header(win, "Rekordbox Dedupe · %s" % os.path.basename(src))
            put(win, 1, 1, "%d gruppi · %d copie da rimuovere · %d ignorati · criterio: %s"
                % (n, to_remove, skipped, opts.keep), col(4))
            list_h = max(4, (h - 7) // 2)
            if gi < gtop:
                gtop = gi
            if gi >= gtop + list_h:
                gtop = gi - list_h + 1
            for r in range(list_h):
                i = gtop + r
                if i >= n:
                    break
                g = an.groups[i]
                a, ti = track_names(g.items[0])
                d = decisions[i]
                sym, c = ("✔", 1) if d == g.auto_keep else (("—", 3) if d is None else ("✎", 4))
                attr = curses.A_REVERSE if i == gi else 0
                line = " %4d  %s  %s – %s" % (i + 1, sym, a, ti)
                tail = " (%d)" % len(g.items)
                put(win, 2 + r, 0, line.ljust(w - 1 - len(tail)) + tail, attr)
                put(win, 2 + r, 8, sym, attr | col(c) | curses.A_BOLD)
            dy = 2 + list_h
            hline(win, dy)
            g = an.groups[gi]
            keep = decisions[gi]
            ci = min(ci, len(g.items) - 1)
            y = dy + 1
            for idx, t in enumerate(g.items):
                tid = t.get("TrackID")
                if keep is None:
                    label, c = "IGNORA ", 3
                elif tid == keep:
                    label, c = "TIENI  ", 1
                else:
                    label, c = "RIMUOVI", 2
                cur = idx == ci
                put(win, y, 1, ">" if cur else " ", curses.A_BOLD)
                put(win, y, 3, label, col(c) | curses.A_BOLD)
                meta = "%-5s %4sk %5s  cue:%-2d  %s  %s" % (
                    fmt_ext(t), t.get("BitRate", "?"), mmss(t), len(t.findall("POSITION_MARK")),
                    "ok" if an.exists[tid] else "FILE MANCANTE", t.get("DateAdded", ""))
                put(win, y, 11, meta, curses.A_BOLD if cur else 0)
                p = location_to_path(t.get("Location"))
                room = w - 8
                put(win, y + 1, 5, ("…" + p[-(room - 1):]) if len(p) > room else p, col(5))
                y += 2
            cur_t = g.items[ci]
            pls = sorted(set(an.membership[cur_t.get("TrackID")]))
            txt = "Playlist della copia selezionata: " + (" | ".join(pls) if pls else "(nessuna)")
            for ln in textwrap.wrap(txt, max(20, w - 4))[:max(1, h - y - 2)]:
                put(win, y, 2, ln)
                y += 1
            if gi < n and g.reason and keep == g.auto_keep:
                pass
            put(win, h - 2, 1, msg, col(3) | curses.A_BOLD)
            put(win, h - 1, 0, " ↑↓ gruppo · ←→ copia · Spazio tieni questa · i ignora · r auto · p playlist · e esporta · s opzioni · ? aiuto · q esci", col(5))
            win.refresh()

            ch = win.getch()
            msg = ""
            if ch in (curses.KEY_DOWN, ord("j")):
                gi, ci = min(n - 1, gi + 1), 0
            elif ch in (curses.KEY_UP, ord("k")):
                gi, ci = max(0, gi - 1), 0
            elif ch == curses.KEY_NPAGE:
                gi, ci = min(n - 1, gi + list_h), 0
            elif ch == curses.KEY_PPAGE:
                gi, ci = max(0, gi - list_h), 0
            elif ch == curses.KEY_HOME:
                gi, ci = 0, 0
            elif ch == curses.KEY_END:
                gi, ci = n - 1, 0
            elif ch == curses.KEY_RIGHT:
                ci = min(len(g.items) - 1, ci + 1)
            elif ch == curses.KEY_LEFT:
                ci = max(0, ci - 1)
            elif ch in (ord(" "), 10, 13, curses.KEY_ENTER):
                decisions[gi] = cur_t.get("TrackID")
            elif ch == ord("i"):
                decisions[gi] = g.auto_keep if decisions[gi] is None else None
            elif ch == ord("r"):
                decisions[gi] = g.auto_keep
            elif ch == ord("R"):
                decisions = default_decisions(an)
                msg = "Tutte le scelte riportate all'automatico."
            elif ch == ord("s"):
                return "setup"
            elif ch == ord("q"):
                return "quit"
            elif ch == ord("?"):
                popup(win, "Aiuto", [
                    "Ogni gruppo e' un brano presente piu' volte in libreria.",
                    "La copia TIENI e' scelta in automatico col criterio delle opzioni.",
                    "",
                    "↑ ↓ / PgUp PgDn / Home End   cambia gruppo",
                    "← →                          seleziona la copia nel gruppo",
                    "Spazio / Invio               tieni QUESTA copia (le altre vengono rimosse)",
                    "i                            ignora il gruppo (non e' un duplicato: non si tocca)",
                    "r / R                        ripristina la scelta automatica (gruppo / tutti)",
                    "p                            anteprima delle playlist che verranno modificate",
                    "e                            esporta XML pulito + report CSV",
                    "s                            torna alle opzioni (rianalizza)",
                    "q / Esc                      esci",
                    "",
                    "Dopo l'export: importa le playlist [dedup] in rekordbox, poi importa",
                    "_DUPLICATI DA ELIMINARE, selezionale tutte e 'Rimuovi dalla collezione'.",
                ])
            elif ch == ord("p"):
                aff = affected_preview(an, decisions)
                lines = ["Playlist con brani da sostituire: %d" % len(aff), ""]
                lines += ["%4d  %s" % (c, p) for p, c in sorted(aff.items())] or ["(nessuna)"]
                popup(win, "Anteprima playlist", lines)
            elif ch == ord("e"):
                out_xml, out_csv = default_outputs(src)
                aff = affected_preview(an, decisions)
                lines = [
                    "Copie da rimuovere : %d" % to_remove,
                    "Gruppi ignorati    : %d" % skipped,
                    "Playlist modificate: %d" % len(aff), "",
                    "Verranno scritti (sovrascritti se esistono):",
                    "  " + out_xml, "  " + out_csv, "",
                    "Invio = esporta · Esc = annulla",
                ]
                ans = popup(win, "Conferma export", lines, footer=" Invio esporta · Esc annulla",
                            keys={10: "y", 13: "y", curses.KEY_ENTER: "y", 27: "n", ord("q"): "n", ord("n"): "n"})
                if ans == "y":
                    win.erase()
                    put(win, 2, 2, "Scrittura in corso…")
                    win.refresh()
                    try:
                        res = apply_decisions(an, decisions, opts)
                        write_outputs(res, out_xml, out_csv)
                    except Exception as e:  # noqa
                        popup(win, "Errore", ["Export fallito:", str(e)])
                        continue
                    lines = ["Fatto.", "", "XML pulito : " + out_xml, "Report CSV : " + out_csv, "",
                             "Brani rimossi dalle playlist: %d" % len(res["removed"]),
                             "Playlist esportate: %d" % len(res["affected"]), ""]
                    lines += ["  %s  (sostituiti %d, doppioni %d)" % (p, a, b)
                              for p, (a, b) in sorted(res["affected"].items())]
                    lines += ["", "Prossimi passi: importa le playlist con suffisso in rekordbox,",
                              "poi importa '_DUPLICATI DA ELIMINARE' e rimuovila dalla collezione."]
                    r = popup(win, "Export completato", lines,
                              footer=" o apri in Finder · Invio/q chiudi",
                              keys={ord("o"): "open", 10: None, 13: None, ord("q"): None, 27: None})
                    if r == "open" and sys.platform == "darwin":
                        subprocess.run(["open", "-R", out_xml])
                    msg = "Export completato."

    # ---- main ---------------------------------------------------------------
    def tui_main(win):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.start_color()
            curses.use_default_colors()
            for i, c in enumerate([curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW,
                                   curses.COLOR_CYAN, curses.COLOR_WHITE], 1):
                curses.init_pair(i, c, -1)
            state["colors"] = True
        except curses.error:
            pass
        win.keypad(True)
        opts = Options()
        path = clean_path(initial_path) if initial_path else ""
        msg = ""
        while True:
            path = screen_setup(win, path, opts, msg)
            if path is None:
                return
            win.erase()
            put(win, 2, 2, "Analisi in corso… (con librerie grandi puo' richiedere qualche secondo)")
            win.refresh()
            try:
                an = analyze(load_xml(path), opts)
            except Exception as e:  # noqa
                msg = "Errore: %s" % e
                continue
            if not an.groups:
                msg = "Nessun duplicato trovato con queste impostazioni."
                continue
            msg = ""
            if screen_groups(win, an, opts, path) == "quit":
                return

    curses.wrapper(tui_main)


# ==========================================================================
#  CLI
# ==========================================================================
def run_cli(args):
    src = os.path.expanduser(args.xml)
    if not os.path.isfile(src):
        sys.exit("File non trovato: %s" % src)
    out_xml, out_csv = default_outputs(src)
    if args.output:
        out_xml = os.path.expanduser(args.output)
    if args.report:
        out_csv = os.path.expanduser(args.report)
    opts = Options(args.keep, args.loose, args.ignore_duration, args.tolerance, args.suffix, args.all_playlists)
    try:
        an = analyze(load_xml(src), opts)
    except ValueError as e:
        sys.exit(str(e))
    res = apply_decisions(an, default_decisions(an), opts)
    write_outputs(res, out_xml, out_csv)
    print("Tracce in collezione: %d" % len(an.tracks))
    print("Gruppi di duplicati: %d  ->  tracce da rimuovere: %d" % (len(an.groups), len(res["removed"])))
    print("\nPlaylist toccate: %d" % len(res["affected"]))
    for p, (a, b) in sorted(res["affected"].items()):
        print("  - %s  (sostituiti: %d, doppioni nella stessa playlist: %d)" % (p, a, b))
    print("\nXML pulito:  %s\nReport CSV:  %s" % (out_xml, out_csv))
    print("\nLa collezione originale NON e' stata modificata.")


def main():
    ap = argparse.ArgumentParser(description="Deduplica una collezione rekordbox (XML) e rimappa le playlist.")
    ap.add_argument("xml", nargs="?", help="XML esportato da rekordbox. Senza argomenti parte l'interfaccia TUI.")
    ap.add_argument("--ui", action="store_true", help="forza l'interfaccia TUI (anche con un file)")
    ap.add_argument("-o", "--output", help="XML di output (CLI)")
    ap.add_argument("--report", help="CSV del report (CLI)")
    ap.add_argument("--keep", choices=KEEP_MODES, default="quality")
    ap.add_argument("--loose", action="store_true", help="ignora le parentesi nel titolo")
    ap.add_argument("--ignore-duration", action="store_true")
    ap.add_argument("--tolerance", type=float, default=5.0)
    ap.add_argument("--all-playlists", action="store_true")
    ap.add_argument("--suffix", default=" [dedup]")
    args = ap.parse_args()
    if args.ui or not args.xml:
        run_tui(args.xml)
    else:
        run_cli(args)


if __name__ == "__main__":
    main()
