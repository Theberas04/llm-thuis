@echo off
rem Bouwt de Windows-versie van de LLM-thuis bureaubladapp.
rem Hetzelfde script werkt lokaal en in de bouwstraat (GitHub Actions).
setlocal
cd /d "%~dp0"

echo == LLM-thuis bureaubladapp bouwen ==
python -m pip install --disable-pip-version-check --quiet --upgrade pyinstaller pillow
if errorlevel 1 goto :fout

python scripts\maak-icoon.py
if errorlevel 1 goto :fout

rem --onedir, geen --onefile: een programma dat zichzelf bijwerkt moet bestanden
rem naast zich kunnen vervangen, en dat kan niet met één opgevouwen bestand.
python -m PyInstaller --noconfirm --clean --windowed --onedir --name "LLM-thuis" ^
  --icon "packaging\merk.ico" ^
  --add-data "version.txt;." ^
  --add-data "..\bord\bord.py;bord" ^
  --add-data "..\assets\merk.png;assets" ^
  main.py
if errorlevel 1 goto :fout

if not exist "dist\LLM-thuis\LLM-thuis.exe" goto :fout
echo Klaar: dist\LLM-thuis\LLM-thuis.exe

rem Tweede vorm: één los .exe-bestand om direct te downloaden. Dit is niet de
rem versie die zichzelf bijwerkt (die zit in de ZIP), maar wel de simpelste weg
rem voor wie alleen even wil kijken.
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "LLM-thuis-alleen" ^
  --icon "packaging\merk.ico" ^
  --add-data "version.txt;." ^
  --add-data "..\bord\bord.py;bord" ^
  --add-data "..\assets\merk.png;assets" ^
  main.py
if errorlevel 1 goto :fout
echo Klaar: dist\LLM-thuis-alleen.exe
exit /b 0

:fout
echo De bouw is mislukt.
exit /b 1
