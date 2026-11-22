"""Het venster van de LLM-thuis bureaubladapp (tkinter, alleen de standaardbibliotheek).

Alles wat met het netwerk praat gebeurt in een aparte draad; de resultaten komen via
een wachtrij terug op de vensterdraad, want tkinter mag alleen vanaf zijn eigen draad
worden aangeraakt. In HEADLESS-modus (voor de zelftest en voor schermafbeeldingen)
komt er geen enkel venster met een knop die op een klik wacht: meldingen gaan dan
naar de statusregel en naar het logboek.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk

from . import core
from .version import VERSIE

# Huisstijl: dezelfde kleuren als het bord en Werdens Monitor.
KLEUREN = {
    "bg": "#f4f7fb",
    "pane": "#ffffff",
    "pane2": "#f7fafd",
    "line": "#e3eaf4",
    "line2": "#cfdaeb",
    "txt": "#0f172a",
    "muted": "#55657f",
    "muted2": "#8296ae",
    "accent": "#2ea3f2",
    "accent_d": "#1e73be",
    "ok": "#059669",
    "amber": "#b45309",
    "red": "#dc2626",
}
MERK = ("#5ff0e8", "#2ea3f2", "#1e73be")

UITGAVEN_PAGINA = f"https://github.com/{core.REPO}/releases/latest"


def kies_font() -> str:
    """Segoe UI op Windows, anders wat er is."""
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families())
    except Exception:  # noqa: BLE001 - zonder letterlijst valt hij terug
        families = set()
    for naam in ("Segoe UI", "Inter", "DejaVu Sans", "Helvetica"):
        if naam in families:
            return naam
    return "TkDefaultFont"


def maak_merk(canvas: tk.Canvas, x: int, y: int, schaal: float = 1.0) -> None:
    """Tekent het merk (drie schuine strepen) op een doek, in de huisstijlkleuren."""
    breedte, hoogte, gat = 13 * schaal, 22 * schaal, 4.5 * schaal
    for i, kleur in enumerate(MERK):
        x0 = x + i * (breedte + gat)
        canvas.create_polygon(
            x0, y - hoogte, x0 + breedte * 0.55, y - hoogte,
            x0 + breedte, y, x0 + breedte * 0.45, y,
            fill=kleur, outline=kleur)


class Venster:
    """Het hoofdvenster met de modellen, de sleutels en de bijwerking."""

    def __init__(self, hoofd: tk.Tk, basis: str | None = None, headless: bool = False):
        self.hoofd = hoofd
        self.headless = headless or os.environ.get("LLM_THUIS_HEADLESS") == "1"
        self.logboek = os.environ.get("LLM_THUIS_LOGBOEK", "")
        self.bord: core.Bord | None = None
        self.bijwerking: core.Bijwerking | None = None
        self.stappen: list[str] = []
        self.wachtrij: queue.Queue = queue.Queue()
        self.aan_het_werk = False
        self._bijwerking_gecontroleerd = False

        gegevens = core.lees_instellingen()
        self.basis_var = tk.StringVar(value=basis or gegevens.get("basis", ""))
        self.sleutel_naam_var = tk.StringVar()
        self.sleutel_budget_var = tk.StringVar(value="5")
        self.status_var = tk.StringVar(value="Klaar om te verbinden.")
        self.doorgeef_var = tk.StringVar(value="")
        self.versie_var = tk.StringVar(value=f"versie {VERSIE}")

        self.font = kies_font()
        self._bouw()
        self.stap("venster")
        self.hoofd.after(150, self._pomp)
        if self.basis_var.get().strip():
            self.hoofd.after(250, self.ververs)
        else:
            self.zet_status("Vul het adres van je bord in en druk op Verversen.")
        self.hoofd.after(700, lambda: self.controleer_bijwerking(bij_start=True))

    # -- opbouw van het venster -------------------------------------------- #
    def _bouw(self) -> None:
        k = KLEUREN
        self.hoofd.configure(bg=k["bg"])
        self.hoofd.title("LLM-thuis")
        self.hoofd.geometry("1060x740")
        self.hoofd.minsize(900, 620)
        self._stijl()

        kop = tk.Frame(self.hoofd, bg=k["pane"], highlightbackground=k["line"], highlightthickness=1)
        kop.pack(fill="x")
        doek = tk.Canvas(kop, width=56, height=34, bg=k["pane"], highlightthickness=0)
        doek.pack(side="left", padx=(18, 8), pady=12)
        maak_merk(doek, 8, 28, 1.0)
        titel = tk.Frame(kop, bg=k["pane"])
        titel.pack(side="left", pady=10)
        tk.Label(titel, text="LLM-thuis", bg=k["pane"], fg=k["txt"],
                 font=(self.font, 17, "bold")).pack(anchor="w")
        tk.Label(titel, text="Status, modellen testen en sleutels beheren vanaf je pc",
                 bg=k["pane"], fg=k["muted"], font=(self.font, 10)).pack(anchor="w")

        rechts = tk.Frame(kop, bg=k["pane"])
        rechts.pack(side="right", padx=18)
        tk.Label(rechts, textvariable=self.versie_var, bg=k["pane"], fg=k["muted2"],
                 font=(self.font, 10)).pack(anchor="e")
        self.knop_bijwerken = ttk.Button(rechts, text="Bijwerken", command=self.bijwerken)
        self.knop_status = tk.Label(rechts, textvariable=self.status_var, bg=k["pane"],
                                    fg=k["muted"], font=(self.font, 10), justify="right",
                                    wraplength=460)

        self.knop_status.pack(anchor="e", pady=(4, 0))

        romp = tk.Frame(self.hoofd, bg=k["bg"])
        romp.pack(fill="both", expand=True, padx=16, pady=14)
        romp.columnconfigure(0, weight=1)
        # De vakken delen de hoogte: zo blijft het sleutelvak zichtbaar op een klein
        # scherm in plaats van onder de onderrand te verdwijnen.
        romp.rowconfigure(1, weight=3)
        romp.rowconfigure(2, weight=2)

        self._adresblok(romp).grid(row=0, column=0, sticky="ew")
        self._modellenblok(romp).grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self._sleutelblok(romp).grid(row=2, column=0, sticky="nsew", pady=(12, 0))

        self.log = tk.Text(self.hoofd, height=5, bg="#0f172a", fg="#dbe7f5", wrap="word",
                           font=("Consolas", 9), relief="flat", padx=10, pady=8)
        self.log.configure(state="disabled")
        self.log.pack(fill="x")

    def _stijl(self) -> None:
        stijl = ttk.Style()
        try:
            stijl.theme_use("clam")
        except tk.TclError:
            pass
        k = KLEUREN
        stijl.configure("TButton", font=(self.font, 10), padding=(12, 6))
        stijl.configure("Hoofd.TButton", font=(self.font, 10, "bold"))
        stijl.configure("TEntry", padding=6)
        stijl.configure("Treeview", font=(self.font, 10), rowheight=26,
                        background=k["pane"], fieldbackground=k["pane"], foreground=k["txt"],
                        bordercolor=k["line"], borderwidth=1)
        stijl.configure("Treeview.Heading", font=(self.font, 10, "bold"),
                        background=k["pane2"], foreground=k["muted"], relief="flat")
        stijl.map("Treeview", background=[("selected", k["accent"])],
                  foreground=[("selected", "#ffffff")])

    def _kaart(self, ouder: tk.Widget, titel: str) -> tk.Frame:
        buiten = tk.Frame(ouder, bg=KLEUREN["pane"], highlightbackground=KLEUREN["line"],
                          highlightthickness=1)
        binnen = tk.Frame(buiten, bg=KLEUREN["pane"])
        binnen.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(binnen, text=titel.upper(), bg=KLEUREN["pane"], fg=KLEUREN["muted2"],
                 font=(self.font, 9, "bold")).pack(anchor="w")
        return buiten, binnen

    def _adresblok(self, ouder: tk.Widget) -> tk.Frame:
        buiten, binnen = self._kaart(ouder, "Je bord")
        rij = tk.Frame(binnen, bg=KLEUREN["pane"])
        rij.pack(fill="x", pady=(8, 0))
        tk.Label(rij, text="Adres", bg=KLEUREN["pane"], fg=KLEUREN["muted"],
                 font=(self.font, 10)).pack(side="left")
        self.adres = ttk.Entry(rij, textvariable=self.basis_var, width=34)
        self.adres.pack(side="left", padx=8)
        self.knop_ververs = ttk.Button(rij, text="Verversen", style="Hoofd.TButton",
                                       command=self.ververs)
        self.knop_ververs.pack(side="left")
        self.knop_open = ttk.Button(rij, text="Open het bord", command=self.open_bord)
        self.knop_open.pack(side="left", padx=8)
        tk.Label(binnen, textvariable=self.doorgeef_var, bg=KLEUREN["pane"], fg=KLEUREN["muted"],
                 font=("Consolas", 9), anchor="w", justify="left").pack(fill="x", pady=(8, 0))
        return buiten

    def _modellenblok(self, ouder: tk.Widget) -> tk.Frame:
        buiten, binnen = self._kaart(ouder, "Modellen")
        self.modelboom = ttk.Treeview(binnen, columns=("naam", "herkomst", "test"),
                                      show="headings", height=5)
        for kolom, kop, breedte in (("naam", "Model", 240), ("herkomst", "Herkomst", 110),
                                    ("test", "Laatste test", 560)):
            self.modelboom.heading(kolom, text=kop)
            self.modelboom.column(kolom, width=breedte, anchor="w")
        self.modelboom.pack(fill="both", expand=True, pady=(8, 8))
        self.modelboom.bind("<Double-1>", lambda _e: self.test_gekozen())

        rij = tk.Frame(binnen, bg=KLEUREN["pane"])
        rij.pack(fill="x")
        self.knop_test = ttk.Button(rij, text="Test het gekozen model", style="Hoofd.TButton",
                                    command=self.test_gekozen)
        self.knop_test.pack(side="left")
        tk.Label(rij, text="De test roept het model echt aan; dat kan even duren.",
                 bg=KLEUREN["pane"], fg=KLEUREN["muted"], font=(self.font, 10)).pack(side="left", padx=10)
        self.uitslag = tk.Text(binnen, height=3, wrap="word", bg=KLEUREN["pane2"],
                               fg=KLEUREN["txt"], relief="flat", padx=10, pady=8,
                               font=(self.font, 10))
        self.uitslag.configure(state="disabled")
        self.uitslag.pack(fill="x", pady=(10, 0))
        return buiten

    def _sleutelblok(self, ouder: tk.Widget) -> tk.Frame:
        buiten, binnen = self._kaart(ouder, "Sleutels met een budget")

        self.inlogrij = tk.Frame(binnen, bg=KLEUREN["pane"])
        self.inlogrij.pack(fill="x", pady=(8, 0))
        tk.Label(self.inlogrij, text="Beheerderswachtwoord", bg=KLEUREN["pane"],
                 fg=KLEUREN["muted"], font=(self.font, 10)).pack(side="left")
        self.wachtwoord = ttk.Entry(self.inlogrij, show="\u2022", width=26)
        self.wachtwoord.pack(side="left", padx=8)
        self.wachtwoord.bind("<Return>", lambda _e: self.inloggen())
        self.knop_inloggen = ttk.Button(self.inlogrij, text="Inloggen", style="Hoofd.TButton",
                                        command=self.inloggen)
        self.knop_inloggen.pack(side="left")
        self.knop_uitloggen = ttk.Button(self.inlogrij, text="Uitloggen", command=self.uitloggen)

        self.sleutelboom = ttk.Treeview(binnen, columns=("naam", "budget", "verbruik", "datum"),
                                        show="headings", height=4)
        for kolom, kop, breedte in (("naam", "Naam", 240), ("budget", "Budget ($)", 120),
                                    ("verbruik", "Verbruikt ($)", 130), ("datum", "Aangemaakt", 160)):
            self.sleutelboom.heading(kolom, text=kop)
            self.sleutelboom.column(kolom, width=breedte, anchor="w")

        self.maakrij = tk.Frame(binnen, bg=KLEUREN["pane"])
        tk.Label(self.maakrij, text="Nieuwe sleutel", bg=KLEUREN["pane"], fg=KLEUREN["muted"],
                 font=(self.font, 10)).pack(side="left")
        self.sleutel_naam = ttk.Entry(self.maakrij, textvariable=self.sleutel_naam_var, width=20)
        self.sleutel_naam.pack(side="left", padx=8)
        tk.Label(self.maakrij, text="budget $", bg=KLEUREN["pane"], fg=KLEUREN["muted"],
                 font=(self.font, 10)).pack(side="left")
        self.sleutel_budget = ttk.Entry(self.maakrij, textvariable=self.sleutel_budget_var, width=8)
        self.sleutel_budget.pack(side="left", padx=(6, 8))
        self.knop_sleutel = ttk.Button(self.maakrij, text="Sleutel maken", style="Hoofd.TButton",
                                       command=self.maak_sleutel)

        self.sleuteluitslag = tk.Text(binnen, height=2, wrap="char", bg="#0f172a", fg="#8ff0e8",
                                      relief="flat", padx=10, pady=6, font=("Consolas", 10))
        self.sleuteluitslag.configure(state="disabled")
        self.knop_kopieer = ttk.Button(binnen, text="Kopieer de sleutel", command=self.kopieer_sleutel)

        self._sleutels_tonen(False)
        return buiten

    # -- klein grut --------------------------------------------------------- #
    def _sleutels_tonen(self, aan: bool) -> None:
        if aan:
            self.inlogrij.pack_forget()
            self.sleutelboom.pack(fill="both", expand=True, pady=(8, 8))
            self.maakrij.pack(fill="x")
            self.knop_sleutel.pack(side="left")
            self.sleuteluitslag.pack(fill="x")
            self.knop_kopieer.pack(anchor="w", pady=(6, 0))
            self.knop_uitloggen.pack(side="left", padx=8)
        else:
            for widget in (self.sleutelboom, self.maakrij, self.sleuteluitslag,
                           self.knop_kopieer, self.knop_uitloggen):
                widget.pack_forget()
            self.inlogrij.pack(fill="x", pady=(8, 0))

    def zet_status(self, tekst: str, soort: str = "gewoon") -> None:
        kleur = {"goed": KLEUREN["ok"], "fout": KLEUREN["red"], "let": KLEUREN["amber"]}.get(
            soort, KLEUREN["muted"])
        self.status_var.set(tekst)
        try:
            self.knop_status.configure(fg=kleur)
        except tk.TclError:
            pass
        self.schrijf(tekst)

    def schrijf(self, tekst: str) -> None:
        """Zet een regel in het logboek, en in de console als die er is."""
        stempel = __import__("time").strftime("%H:%M:%S")
        regel = f"[{stempel}] {tekst}"
        try:
            self.log.configure(state="normal")
            self.log.insert("end", regel + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except tk.TclError:
            pass
        if self.logboek:
            try:
                with open(self.logboek, "a", encoding="utf-8") as bestand:
                    bestand.write(regel + "\n")
            except OSError:
                pass

    def stap(self, naam: str) -> None:
        """Onthoudt hoe ver de zelftest kwam (voor het verslag)."""
        self.stappen.append(naam)

    def melding(self, titel: str, tekst: str, soort: str = "info") -> None:
        """Eén plek voor meldingen: in HEADLESS-modus nooit een venster dat wacht."""
        self.schrijf(f"{titel}: {tekst}")
        if self.headless:
            return
        try:
            from tkinter import messagebox
            if soort == "fout":
                messagebox.showerror(titel, tekst)
            elif soort == "vraag":
                messagebox.showwarning(titel, tekst)
            else:
                messagebox.showinfo(titel, tekst)
        except Exception as fout:  # noqa: BLE001 - een melding mag nooit de app stoppen
            self.schrijf(f"(melding kon niet worden getoond: {fout})")

    # -- draden en de wachtrij --------------------------------------------- #
    def _start(self, naam: str, werk, klaar=None) -> None:
        """Draait 'werk' in een aparte draad en geeft het resultaat aan 'klaar'."""
        if self.aan_het_werk:
            self.zet_status("Er loopt al een opdracht; wacht tot die klaar is.", "let")
            return
        self.aan_het_werk = True
        self.knop_ververs.configure(state="disabled")

        def loop() -> None:
            try:
                self.wachtrij.put((naam, True, werk(), klaar))
            except Exception as fout:  # noqa: BLE001 - fouten horen bij de gebruiker
                self.wachtrij.put((naam, False, fout, klaar))

        threading.Thread(target=loop, name=f"werk-{naam}", daemon=True).start()

    def _pomp(self) -> None:
        try:
            while True:
                naam, gelukt, uitkomst, klaar = self.wachtrij.get_nowait()
                self.aan_het_werk = False
                try:
                    self.knop_ververs.configure(state="normal")
                except tk.TclError:
                    pass
                if gelukt:
                    try:
                        if klaar:
                            klaar(uitkomst)
                    except Exception as fout:  # noqa: BLE001
                        self.zet_status(f"Onverwachte fout: {fout}", "fout")
                else:
                    self.zet_status(str(uitkomst), "fout")
                    if naam == "bijwerken":
                        self.melding("Bijwerken mislukt", str(uitkomst), "fout")
        except queue.Empty:
            pass
        try:
            self.hoofd.after(150, self._pomp)
        except tk.TclError:
            pass

    # -- het bord ----------------------------------------------------------- #
    def _verbind(self) -> core.Bord:
        adres = core.normaliseer_basis(self.basis_var.get())
        if self.bord is None or self.bord.basis != adres:
            self.bord = core.Bord(adres)
        return self.bord

    def ververs(self) -> None:
        try:
            bord = self._verbind()
        except core.BordFout as fout:
            self.zet_status(str(fout), "let")
            return
        self.basis_var.set(bord.basis)
        self.zet_status("Bezig met ophalen…")
        self._start("ververs", bord.status, self._toon_status)

    def _toon_status(self, gegevens: dict) -> None:
        self.modelboom.delete(*self.modelboom.get_children())
        for model in gegevens.get("modellen") or []:
            laatste = model.get("laatste_test") or {}
            uitslag = ""
            if laatste:
                uitslag = (f"{laatste.get('antwoord', '')[:70]}"
                           if laatste.get("gelukt") else f"mislukt: {laatste.get('antwoord', '')[:50]}")
                if laatste.get("tijd"):
                    uitslag += f"  ({laatste['tijd']})"
            self.modelboom.insert("", "end", values=(model.get("naam", "?"),
                                                     model.get("herkomst", ""), uitslag))
        basis_url = gegevens.get("basis_url") or ""
        self.doorgeef_var.set(f"Dit adres zet je in je apps:  {basis_url}"
                              f"\nDe gateway draait op {gegevens.get('gateway', '?')} — "
                              f"{'levend' if gegevens.get('gateway_levend') else 'niet bereikbaar'}"
                              f" ({gegevens.get('gateway_detail', '')})"
                              f"\nSleutelbeheer: {'aan' if gegevens.get('sleutels_aan') else 'uit'}")
        self.versie_var.set(f"versie {VERSIE}  ·  bord {gegevens.get('versie', '?')}")
        aantal = len(gegevens.get("modellen") or [])
        self.zet_status(f"{aantal} model(len) gevonden op {self.bord.basis}."
                        if self.bord else "Bijgewerkt.", "goed")
        core.bewaar_instellingen({"basis": self.bord.basis if self.bord else self.basis_var.get(),
                                 "laatst_bijgewerkt": __import__("time").time()})

    def test_gekozen(self) -> None:
        keuze = self.modelboom.selection()
        if not keuze:
            self.zet_status("Kies eerst een model in de lijst.", "let")
            return
        model = self.modelboom.item(keuze[0], "values")[0]
        try:
            bord = self._verbind()
        except core.BordFout as fout:
            self.zet_status(str(fout), "let")
            return
        self.zet_status(f"{model} wordt getest… dit kan even duren.")
        self._start("test", lambda: bord.test_model(model), self._toon_test)

    def _toon_test(self, uitslag: dict) -> None:
        self.uitslag.configure(state="normal")
        self.uitslag.delete("1.0", "end")
        if uitslag.get("gelukt"):
            self.uitslag.insert("end", f"{uitslag.get('model')} antwoordde in "
                                      f"{uitslag.get('seconden', '?')} s")
            if uitslag.get("tokens"):
                self.uitslag.insert("end", f" ({uitslag['tokens']} tokens)")
            self.uitslag.insert("end", f":\n{uitslag.get('antwoord', '')}")
            self.zet_status(f"{uitslag.get('model')} werkt.", "goed")
        else:
            self.uitslag.insert("end", f"{uitslag.get('model')} mislukte:\n"
                                      f"{uitslag.get('error') or uitslag.get('antwoord', '')}")
            self.zet_status(f"{uitslag.get('model')} gaf geen antwoord.", "fout")
        self.uitslag.configure(state="disabled")

    def open_bord(self) -> None:
        try:
            adres = core.normaliseer_basis(self.basis_var.get())
        except core.BordFout as fout:
            self.zet_status(str(fout), "let")
            return
        webbrowser.open(adres)

    # -- sleutels ----------------------------------------------------------- #
    def inloggen(self) -> None:
        wachtwoord = self.wachtwoord.get()
        if not wachtwoord:
            self.zet_status("Vul het beheerderswachtwoord in.", "let")
            return
        try:
            bord = self._verbind()
        except core.BordFout as fout:
            self.zet_status(str(fout), "let")
            return
        self.zet_status("Inloggen…")
        self._start("inloggen", lambda: bord.inloggen(wachtwoord), lambda _r: self._na_inloggen())

    def _na_inloggen(self) -> None:
        self.wachtwoord.delete(0, "end")
        self._sleutels_tonen(True)
        self.zet_status("Ingelogd als beheerder.", "goed")
        self.sleutels_laden()

    def uitloggen(self) -> None:
        if self.bord:
            self.bord.uitloggen()
        self._sleutels_tonen(False)
        self.zet_status("Uitgelogd.")

    def sleutels_laden(self) -> None:
        if not self.bord:
            return
        self._start("sleutels", self.bord.sleutels, self._toon_sleutels)

    def _toon_sleutels(self, rijen: list[dict]) -> None:
        self.sleutelboom.delete(*self.sleutelboom.get_children())
        for rij in rijen:
            self.sleutelboom.insert("", "end", values=(
                rij.get("naam", "?"),
                self._geld(rij.get("budget")),
                self._geld(rij.get("verbruik")),
                rij.get("aangemaakt", "")))
        self.zet_status(f"{len(rijen)} sleutel(s) gevonden.", "goed")

    @staticmethod
    def _geld(waarde) -> str:
        if waarde in (None, ""):
            return "—"
        try:
            return f"{float(waarde):.2f}"
        except (TypeError, ValueError):
            return str(waarde)

    def maak_sleutel(self) -> None:
        if not self.bord:
            return
        naam = self.sleutel_naam_var.get().strip()
        if not naam:
            self.zet_status("Geef de nieuwe sleutel een naam.", "let")
            return
        ruw = self.sleutel_budget_var.get().strip().replace(",", ".")
        budget = None
        if ruw:
            try:
                budget = float(ruw)
            except ValueError:
                self.zet_status("Het budget moet een getal zijn, bijvoorbeeld 5.", "let")
                return
            if budget <= 0:
                self.zet_status("Het budget moet groter dan nul zijn.", "let")
                return
        self.zet_status(f"Sleutel '{naam}' wordt gemaakt…")
        self._start("sleutel", lambda: self.bord.maak_sleutel(naam, budget), self._toon_nieuwe_sleutel)

    def _toon_nieuwe_sleutel(self, uitslag: dict) -> None:
        sleutel = uitslag.get("sleutel", "")
        self.sleuteluitslag.configure(state="normal")
        self.sleuteluitslag.delete("1.0", "end")
        self.sleuteluitslag.insert("end", sleutel)
        self.sleuteluitslag.configure(state="disabled")
        self.sleutel_naam_var.set("")
        self.zet_status(f"Sleutel '{uitslag.get('naam')}' gemaakt — bewaar hem nu, "
                        f"je ziet hem hierna niet meer.", "goed")
        self.sleutels_laden()

    def kopieer_sleutel(self) -> None:
        sleutel = self.sleuteluitslag.get("1.0", "end").strip()
        if not sleutel:
            self.zet_status("Er is nog geen nieuwe sleutel om te kopiëren.", "let")
            return
        self.hoofd.clipboard_clear()
        self.hoofd.clipboard_append(sleutel)
        self.zet_status("De sleutel staat op het klembord.", "goed")

    # -- bijwerken ---------------------------------------------------------- #
    def controleer_bijwerking(self, bij_start: bool = False, poging: int = 0) -> None:
        # De bijwerkcontrole deelt de wachtrij met het bord, dus bij het starten kan
        # het verversen nog lopen. Wachten en het opnieuw proberen is beter dan de
        # gebruiker met "er loopt al een opdracht" op te zadelen.
        if self.aan_het_werk and poging < 20:
            self.hoofd.after(800, lambda: self.controleer_bijwerking(bij_start, poging + 1))
            return
        self._bijwerking_gecontroleerd = True
        self.stap("bijwerking-controleren")

        def werk():
            manifest = core.lees_manifest()
            return core.controleer_bijwerking(VERSIE, manifest=manifest)

        self._start("controleer-bijwerking", werk,
                    lambda nieuw: self._bijwerking_gevonden(nieuw, bij_start))

    def _bijwerking_gevonden(self, nieuw, bij_start: bool) -> None:
        if not nieuw:
            if not bij_start:
                self.zet_status(f"Je hebt de nieuwste versie ({VERSIE}).", "goed")
            self.knop_bijwerken.pack_forget()
            self.stap("bijwerking-bij")
            return
        self.bijwerking = nieuw
        self.stap(f"bijwerking-beschikbaar:{nieuw.versie}")
        self.knop_bijwerken.configure(
            text=f"Bijwerken naar {nieuw.versie}" if core.kan_bijwerken()
            else f"Nieuwe versie {nieuw.versie} ophalen")
        self.knop_bijwerken.pack(anchor="e", pady=(4, 0))
        self.zet_status(f"Versie {nieuw.versie} is beschikbaar.", "let")
        if bij_start:
            self.schrijf(f"Versie {nieuw.versie} staat klaar ({nieuw.bestand}).")
        if not core.kan_bijwerken() and self.bijwerking:
            self.knop_bijwerken.configure(command=lambda: webbrowser.open(UITGAVEN_PAGINA))

    def bijwerken(self) -> None:
        if not self.bijwerking:
            self.controleer_bijwerking()
            return
        nieuw = self.bijwerking
        if not core.kan_bijwerken():
            webbrowser.open(UITGAVEN_PAGINA)
            return
        if not self.headless:
            from tkinter import messagebox
            if not messagebox.askyesno("Bijwerken",
                                       f"Versie {nieuw.versie} ophalen en installeren?\n\n"
                                       "De app sluit daarna en start opnieuw."):
                return
        self.zet_status(f"Versie {nieuw.versie} wordt opgehaald…")
        self.stap("bijwerken-starten")

        def klaar(uitslag: dict) -> None:
            self.stap("bijwerken-doorgezet")
            self.melding("Bijwerken", "De nieuwe versie is opgehaald en wordt geplaatst. "
                                      "De app sluit nu en start zo opnieuw.", "info")
            self.hoofd.after(600, self.hoofd.destroy)

        self._start("bijwerken", lambda: core.zet_bijwerking_door(nieuw), klaar)

    # -- zelftest ----------------------------------------------------------- #
    def sluit(self) -> None:
        try:
            self.hoofd.destroy()
        except tk.TclError:
            pass
