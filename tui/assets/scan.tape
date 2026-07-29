# Zinthos — local library scan demo (point at a folder → taste map + recommendations)
# Render from the REPO ROOT:
#   nix develop .#tui -c vhs tui/assets/scan.tape
# Same prerequisites as demo.tape (engine up + release build). The scanned folder's
# track titles + taste breakdown appear in the GIF — pick a folder you're happy to publish.

Output docs/scan.gif

Set Shell "bash"
Set FontSize 22
Set Width 1500
Set Height 840
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 75ms
Set CursorBlink false

# ── launch (hidden) ──
Hide
Type "tui/target/release/sonic-tui"
Enter
Sleep 3s
Show

# ── menu → Scan Library (tile 2) ──
Sleep 2s
Type "2"
Sleep 800ms

# ── point it at a folder of audio files ──
Type "/home/zer0/MusicSample"
Sleep 600ms
Enter

# ── scan: read local tags → identity-match against the 255M catalog ──
# ~40-track hardlinked sample (see assets/CLAUDE.md) so this finishes fast. If the
# GIF cuts off mid-scan, raise this; if it sits idle at the end, lower it.
Sleep 6s

# ── walk the matched library + taste breakdown ──
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 600ms
Down
Sleep 1s

# ── open a matched track, then "more like this" ──
Enter
Sleep 2500ms
Type "s"
Sleep 3s
Down
Sleep 600ms
Down
Sleep 1500ms

Hide
Type "q"
Sleep 300ms
Ctrl+C
