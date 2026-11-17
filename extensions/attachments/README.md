# Załączniki dla Knowledge Base MCP

Rozszerzenie obrazu `obsidian-remote-mcp` 2.1.2 przypiętego do digestu
`sha256:82c3892e6fc42453441a7da5cae63260c446d373a54c766e2e6e7b5c12f92cc6`.
Korzysta z istniejącego serwera, OAuth, API key, kontroli ścieżek, rewizji,
blokad i dziennika operacji. Nie zmienia plików instrukcji ani konfiguracji klienta.

## Nowe narzędzia

- `inspect_attachment_tool`: metadane i rewizja pliku, bez pobierania jego zawartości.
- `move_attachment_tool`: podgląd i przenoszenie pojedynczego załącznika z aktualizacją linków.
- `delete_attachment_tool`: podgląd i usuwanie pojedynczego załącznika; domyślnie do kosza.
- `restore_attachment_tool`: przywracanie z kosza bez nadpisywania pliku docelowego.

Operacje przenoszenia/usuwania domyślnie mają `dry_run=true`. Z podglądu
należy przekazać `revision` jako `expected_revision` do wywołania z `dry_run=false`.
Usuwanie pliku używanego w notatkach domyślnie jest blokowane. Opcjonalne
`allow_broken_links=true` świadomie pozostawia odnośniki w zapisywalnych notatkach.
Odnośniki w chronionych notatkach blokują operację również z tym parametrem.

Wymagane istniejące flagi: `ENABLE_MOVE=true` i `ENABLE_DELETE=true`.
`ALLOW_PERMANENT_DELETE=true` umożliwia dodatkowo usunięcie z `trash=false`.
`WRITE_PATHS=Hybrid/,AI/` i ochrona `Human/` pozostają obowiązujące.

## Obsługiwane odnośniki

Wikilinki i osadzenia `[[...]]` / `![[...]]`, aliasy, rozmiary oraz fragmenty;
standardowe linki i obrazy Markdown, ścieżki względne, kodowanie URL,
definicje linków referencyjnych. Nowe wikilinki używają pełnej ścieżki od vaulta.
Linki Markdown zachowują ścieżkę względną względem danej notatki.

Niejednoznaczne nazwy, surowe odnośniki YAML/HTML i odniesienia w Canvas,
Bases lub innych rozpoznanych plikach tekstowych powodują odmowę przed zapisem.
To celowa granica pierwszej wersji: należy najpierw uporządkować takie odniesienie.
Analiza obejmuje pliki widoczne dla danego połączenia MCP, nie wykluczone katalogi
ani wnętrze plików binarnych. Przenoszenie nie zmienia rozszerzenia pliku.

## Wdrożenie

1. Na knowledge-base zbuduj obraz z tego katalogu:

   ```sh
   docker build -t local/kb-mcp-attachments:2.1.2-1 .
   ```

   Budowa wykonuje testy na izolowanych plikach tymczasowych jako UID 1000.
   Błąd testów przerywa budowę; działający kontener i prawdziwy vault nie są używane.

2. W istniejącym stacku Dockhand zmień tylko obraz usługi MCP:

   ```yaml
   image: local/kb-mcp-attachments:2.1.2-1
   pull_policy: never
   ```

   Zachowaj zmienne OAuth i wszystkie pozostałe ustawienia. Usuń ewentualne
   nadpisanie `entrypoint`/`command`, jeśli było ręcznie dodane. Wyłącz pobieranie
   obrazu z rejestru dla tego lokalnego obrazu.

   Obowiązuje wspólny mount `/srv/knowledge-base/vault:/vault:rw`, osobny
   `/srv/knowledge-base/vault/Human:/vault/Human:ro` i istniejący mount `/data`.
   Osobne mounty AI i Hybrid uniemożliwiłyby atomowe przenoszenie między nimi.

3. Wdróż ponownie stack i sprawdź logi kontenera. W ai-docker:

   ```sh
   docker exec --user hermes hermes-agent hermes mcp test kb
   docker restart -t 60 hermes-agent hermes-webui
   ```

   Test odkrywania powinien pokazać cztery nowe narzędzia. Restart wykonaj po
   zakończeniu bieżących zadań Hermesa, potem zacznij nową rozmowę.

## Próba po wdrożeniu

Użyj wyłącznie nowego, jednorazowego załącznika testowego i nowej notatki w AI.
Wykonaj podgląd i przeniesienie do Hybrid, sprawdź aktualizację osadzenia oraz
zawartość pliku. Usuń link z notatki testowej, przenieś załącznik do kosza i go
przywróć. Potwierdź synchronizację w Obsidianie. Nie używaj do testu własnych zdjęć.

## Ograniczenia spójności

Rewizje wykrywają zmiany plików przed zapisem, a blokady koordynują narzędzia MCP.
LiveSync i Obsidian nie uczestniczą w tych blokadach. Operacja obejmująca kilka
plików nie jest transakcją odporną na przerwanie zasilania. Obsłużony błąd zapisu
uruchamia wycofanie; jeśli równoczesna edycja uniemożliwi wycofanie, narzędzie
zgłosi konkretne ścieżki do kontroli zamiast nadpisywać nowszą zawartość.
Podczas większego porządkowania unikaj równoczesnej edycji tych samych notatek.

## Powrót do poprzedniej wersji

Przywróć poprzedni `image` z digestem podanym powyżej, usuń `pull_policy: never`
i ponownie wdróż stack. OAuth, dane i ustawienia pozostają w dotychczasowych mountach.
Wycofanie obrazu nie cofa operacji na plikach wykonanych już przez użytkownika.

## Źródło

https://github.com/ykoellmann/obsidian-mcp/tree/v2.1.2

Rozszerzenie jest lokalną modyfikacją; aktualizacja bazowego MCP wymaga ponownego
sprawdzenia zgodności i testów, nie automatycznej podmiany na `latest`.
