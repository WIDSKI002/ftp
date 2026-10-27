# CloudVault

Prywatny, jednoosobowy menedżer Google Drive i MEGA z trwałymi notatkami. Interfejs jest po polsku, responsywny i ma jasny/ciemny motyw. Backend nie udostępnia rejestracji.

## Architektura i rzeczywisty zakres integracji

- **Python 3.11+ / Flask / SQLite lokalnie albo PostgreSQL na hostingu** — serwer, notatki i ulubione.
- **Google Drive API v3 + OAuth 2.0** — oficjalne API Google. Token odświeżania jest szyfrowany kluczem Fernet przed zapisaniem w bazie; aplikacja nie pyta o hasło Google. Używa scope `drive`, bo aplikacja zarządza plikami, które użytkownik wybierze w swoim prywatnym koncie.
- **MEGA przez oficjalny MEGAcmd** — oficjalna dokumentacja MEGA opisuje MEGAcmd jako skryptowalne narzędzie oraz jego serwer WebDAV. Aplikacja rozmawia z lokalnym WebDAV MEGAcmd (PROPFIND, GET, PUT, MKCOL, MOVE, DELETE); nie używa nieoficjalnej biblioteki Node ani nieimplementuje własnej kryptografii MEGA. MEGAcmd musi działać i być zalogowany na tym samym komputerze/konto systemowym co CloudVault.
- **SQLite lokalnie / PostgreSQL na Renderze** — notatki, tokeny i ulubione są trwale zapisywane w skonfigurowanej bazie. Bezpłatny Render ma tymczasowy system plików, więc na hostingu używaj zewnętrznej bazy Neon przez `DATABASE_URL`, a nie lokalnego SQLite.

MEGA WebDAV nie podaje wiarygodnego limitu wolnego miejsca konta. Dlatego aplikacja nie zgaduje: automatyczny wybór używa wolnego miejsca zwracanego przez Drive oraz opcjonalnego `MEGA_FREE_BYTES`. Jeżeli nie da się ustalić, że choć jeden dysk ma miejsce na cały plik, aplikacja zatrzymuje przesyłanie i prosi o ręczny wybór. Pojedyncze pliki nigdy nie są dzielone między dyski. Zewnętrzne konta Google/MEGA i uprawnienia OAuth wymagają konfiguracji właściciela; testy nie podszywają się pod prawdziwe konto.

## Uruchomienie lokalne (Windows)

1. Zainstaluj Python 3.11 lub nowszy.
2. W PowerShell, z katalogu projektu:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. Wygeneruj sekret sesji, hasło administratora i klucz szyfrowania tokenów:

   ```powershell
   python -c "import secrets; print(secrets.token_hex(32))"
   python app.py hash-password
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Wstaw wyniki do `SECRET_KEY`, `ADMIN_PASSWORD_HASH` i `TOKEN_ENCRYPTION_KEY` w `.env`. Użyj własnego silnego hasła o długości co najmniej 12 znaków. Nie wpisuj hasła jawnego do `.env`, nie publikuj `.env` i nie używaj wartości z przykładu.

4. Dla lokalnego testowania przez `http://127.0.0.1:5000` ustaw `COOKIE_SECURE=false`. Uruchom:

   ```powershell
   python app.py
   ```

   Otwórz `http://127.0.0.1:5000`. Serwer developerski nasłuchuje wyłącznie na `127.0.0.1`. Nie wystawiaj go bezpośrednio do internetu.

## Google Drive

1. Utwórz projekt w [Google Cloud Console](https://console.cloud.google.com/), włącz **Google Drive API** i skonfiguruj ekran zgody OAuth.
2. Dodaj klienta OAuth typu **Web application**. W **Authorized redirect URIs** wpisz dokładnie publiczny adres aplikacji zakończony `/oauth/google/callback`.
3. Umieść `Client ID`, `Client secret` i ten sam adres przekierowania w `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` oraz `GOOGLE_REDIRECT_URI`.
4. Zaloguj się do CloudVault i w **Ustawienia → Google Drive → Połącz Drive** zatwierdź dostęp.

Tryb testowy ekranu zgody Google może ograniczać dostęp do wskazanych kont testowych. Publikacja OAuth może podlegać weryfikacji Google. Nie wyłączaj ostrzeżeń ani nie udostępniaj sekretu klienta.

## MEGA przez MEGAcmd

1. Zainstaluj MEGAcmd wyłącznie z [oficjalnej strony MEGA](https://mega.io/cmd).
2. Uruchom MEGAcmd na koncie systemowym, pod którym działa CloudVault, i zaloguj konto z poziomu MEGAcmd. Uwierzytelnienie MEGA pozostaje zarządzane przez MEGAcmd — aplikacja nie przechowuje hasła do MEGA.
3. W MEGAcmd uruchom `webdav /`. Dokumentacja MEGA opisuje domyślny serwer WebDAV jako lokalny (`127.0.0.1:4443`) i wskazuje możliwość przeglądania, edycji, kopiowania i usuwania plików. Skopiuj wyświetlony adres, łącznie z losową ścieżką, do `MEGA_WEBDAV_URL`.
4. Nie używaj opcji `--public`; nie otwieraj portu WebDAV w zaporze i pozostaw usługę osiągalną tylko lokalnie. CloudVault odrzuca adresy WebDAV spoza localhost. MEGAcmd musi pozostawać uruchomiony; po restarcie sprawdź, czy sesja i udostępniona ścieżka WebDAV nadal działają.
5. Jeśli chcesz używać automatycznego wyboru dla MEGA, wpisz ręcznie oszacowaną, aktualną wolną przestrzeń (w bajtach) w `MEGA_FREE_BYTES`. W przeciwnym razie wybierz MEGA ręcznie.

W dokumentacji MEGA wersja MEGAcmd 2.7.0 opisuje WebDAV i wymienia przykładowy adres `http://127.0.0.1:4443/...`. Dostępność poleceń lub zachowanie WebDAV zależy od zainstalowanej wersji. Przed użyciem na ważnych plikach przetestuj tworzenie folderu, przesyłanie, pobieranie, zmianę nazwy, przenoszenie i usuwanie na plikach testowych.

## Darmowe wdrożenie na Render + Neon (MEGA wyłączone)

Gotowa konfiguracja znajduje się w [`render.yaml`](./render.yaml). Render Free może uruchomić aplikację i zapewnia adres HTTPS `*.onrender.com`, ale usypia usługę po 15 minutach bez ruchu; pierwsze wejście po przerwie może czekać około minuty. Darmowy plan ma też limity zasobów, miesięcznych godzin i transferu. To wariant hobbystyczny, bez gwarancji dostępności.

Render Free ma nietrwały lokalny dysk i nie obsługuje persistent disks. **Nie zapisuj bazy SQLite na Renderze.** Utwórz bazę PostgreSQL w [Neon](https://neon.com/), którego plan Free według aktualnej oferty nie wymaga karty. Baza Neon może usypiać się po bezczynności; skonfiguruj kopie zapasowe i sprawdzaj aktualne limity dostawcy.

**MEGA jest celowo wyłączone w konfiguracji Render Free** (`MEGA_ENABLED=false`). Obecny adapter wymaga procesu MEGAcmd i lokalnego WebDAV, których nie uruchamiamy jako trwałej usługi na Renderze. Google Drive nadal działa po skonfigurowaniu OAuth. Użyj VPS-a lub innego hosta z długotrwale działającym MEGAcmd, jeśli MEGA jest konieczne.

### Przygotowanie kont i wdrożenie

1. Utwórz konto GitHub (nie wymaga karty), utwórz prywatne repozytorium i opublikuj pliki projektu. **Nie dodawaj `.env`, haseł, tokenów ani kluczy** do repozytorium.
2. Utwórz konto na [Neon](https://neon.com/), utwórz PostgreSQL project w odpowiednim regionie i skopiuj connection string z sekcji połączeń (najlepiej wariant pooled, jeśli Neon go oferuje). Zachowaj go prywatnie; będzie on `DATABASE_URL`.
3. W PowerShellzie w katalogu projektu zainstaluj zależności, wygeneruj hash hasła i klucz Fernet. Do wartości Rendera kopiuj wyłącznie wygenerowany hash i klucz, nie jawne hasło:

   ```powershell
   python -m pip install -r requirements.txt
   python app.py hash-password
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

4. W [Render Dashboard](https://dashboard.render.com/) wybierz **New → Blueprint**, połącz repozytorium i zatwierdź `render.yaml`. Wartości `SECRET_KEY` Render generuje sam. Ustaw prywatnie wymagane zmienne Blueprint: `ADMIN_PASSWORD_HASH`, `TOKEN_ENCRYPTION_KEY`, `DATABASE_URL`. Nie dodawaj zmiennych z danymi logowania do repo.
5. Poczekaj na pierwsze wdrożenie i otwórz adres usługi `https://<nazwa>.onrender.com`. Dodaj go do zakładek na pozostałych urządzeniach.
6. Aby podłączyć Drive, utwórz OAuth client typu Web application w Google Cloud Console, a jako Authorized redirect URI oraz `GOOGLE_REDIRECT_URI` ustaw dokładnie `https://<nazwa>.onrender.com/oauth/google/callback`. W Render Dashboard w **Environment** wpisz `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` i `GOOGLE_REDIRECT_URI`, zapisz i wdroż ponownie. Zaloguj się do CloudVault i połącz Drive w Ustawieniach.
7. Sprawdź logowanie, utwórz testową notatkę, odśwież stronę i potwierdź, że notatka nadal jest widoczna. Wykonaj też kopię bazy Neon zgodnie z aktualną ofertą dostawcy.

Plik Blueprint ustawia limit uploadu na 25 MB, aby ograniczyć zużycie pamięci darmowej usługi. Adres `onrender.com` i HTTPS są dostarczane bez kupowania domeny. Zmiana kodu w gałęzi GitHub podłączonej do Render wywołuje kolejne wdrożenie.

Lokalne notatki, ulubione i połączenie Google zapisane w SQLite nie są automatycznie kopiowane do nowej bazy Neon. Jeśli masz już ważne dane, zabezpiecz je i przenieś przed przełączeniem hostingu; Google Drive trzeba będzie ponownie połączyć.

## Wdrożenie na własnym VPS-ie z HTTPS

Do stałej pracy wymagany jest serwer, który zapewnia **trwały dysk** dla SQLite i katalogu stanu MEGAcmd. Bezpłatne plany często usypiają aplikację lub mają nietrwały system plików; w takim przypadku notatki i sesja MEGAcmd mogą zniknąć, więc nie są odpowiednim miejscem na tę instalację. Nie zakładaj, że darmowy hosting utrzyma MEGAcmd w tle.

Zalecana konfiguracja: prywatny VPS z trwałym dyskiem, CloudVault i MEGAcmd pod dedykowanym kontem systemowym, zapora wpuszczająca tylko HTTPS, reverse proxy (np. Caddy) z automatycznym TLS i usługą działającą wyłącznie na `127.0.0.1:5000`.

1. Skopiuj kod na serwer Linux i zainstaluj Python 3.11+, MEGAcmd, Caddy oraz zależności z `requirements.txt`.
2. Utwórz i zabezpiecz `.env` (uprawnienia tylko dla użytkownika aplikacji). Ustaw `COOKIE_SECURE=true`, produkcyjny `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `TOKEN_ENCRYPTION_KEY`, adresy OAuth i, opcjonalnie, `MEGA_WEBDAV_URL`.
3. Zainstaluj MEGAcmd, zaloguj prywatne konto pod użytkownikiem usługi, wykonaj `webdav /`, a potem skopiuj adres do `.env`. Nie włączaj `--public`.
4. Uruchom serwer WSGI, a nie `python app.py`:

   ```sh
   . .venv/bin/activate
   waitress-serve --call --listen=127.0.0.1:5000 app:create_app
   ```

   Skonfiguruj tę komendę jako usługę systemd uruchamianą przy starcie. Katalog roboczy musi wskazywać katalog projektu, a `.env` i stan MEGAcmd muszą być dostępne użytkownikowi usługi. Zewnętrzny reverse proxy musi przekazywać host i HTTPS; ogranicz ruch do własnej domeny.

5. W Caddy ustaw domenę i reverse proxy do `127.0.0.1:5000`, np.:

   ```caddy
   cloud.twoja-domena.example {
       reverse_proxy 127.0.0.1:5000
   }
   ```

   Caddy pobiera certyfikat TLS automatycznie, jeśli domena wskazuje na serwer i porty 80/443 są osiągalne. Ustaw ten publiczny adres jako `GOOGLE_REDIRECT_URI`, zrestartuj usługę i dodaj identyczny URI w Google Cloud Console.

Krótki adres wymaga domeny. Można użyć własnej domeny i bezpłatnego DNS albo własnej domeny podpiętej do Cloudflare; bezpłatne tymczasowe tunele nie gwarantują stałego, krótkiego adresu i nie zastępują trwałego serwera. Nie wystawiaj aplikacji ani WebDAV przez przypadkowy publiczny tunel.

## Bezpieczeństwo, kopie zapasowe i ograniczenia

- Skonfiguruj HTTPS przed publicznym udostępnieniem. Produkcyjne ciasteczko sesji ma flagi `HttpOnly`, `Secure`, `SameSite=Lax` (wymagane dla bezpiecznego powrotu z OAuth); każde żądanie modyfikujące dane wymaga tokenu CSRF.
- Dostęp ma jedno konto. Hasło jest w konfiguracji tylko w postaci scrypt; sesje są podpisane, a token Google szyfrowany osobnym kluczem. Zabezpiecz `.env`, bazę danych i stan MEGAcmd tak samo jak dane użytkownika.
- Ustaw `MAX_UPLOAD_MB` adekwatnie do RAM i limitów reverse proxy. Interfejs pokazuje postęp przesyłania pliku z przeglądarki do serwera; transfer z serwera do Google/MEGA odbywa się w tym samym żądaniu. Google Drive używa przesyłania resumable. Utrata połączenia może wymagać ponowienia całego pliku.
- Google Drive zwraca limit i zużycie przez API, jeśli konto go udostępnia. MEGA przez WebDAV nie zapewnia tego pomiaru. W razie limitu nieznanego interfejs go nie fabrykuje.
- CloudVault nie jest publicznym serwisem udostępniania linków i nie obsługuje wielu kont, synchronizacji lokalnych katalogów, historii wersji ani współdzielenia między użytkownikami.
- Notatki są automatycznie zapisywane po stronie serwera w SQLite (lokalnie) lub PostgreSQL (Render), nigdy w `localStorage`. Wykonuj regularne kopie bazy i przechowuj je bezpiecznie, osobno od serwera.

## Testy

```powershell
python -m unittest discover -s tests -v
```

Testy obejmują logowanie, ochronę CSRF, trwałe notatki, wyszukiwanie i przypinanie, ulubione, wybór miejsca, upload oraz operacje adapterów plików. Adaptery chmur są w testach zastępowane atrapami; nie udają testu prawdziwego konta. Rzeczywiste OAuth Google wymaga konta i konfiguracji Google Cloud, a rzeczywiste operacje MEGA wymagają zalogowanego MEGAcmd. Po skonfigurowaniu wykonaj ręczny test pliku testowego przed użyciem danych produkcyjnych.
