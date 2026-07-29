# Zinthos — find-similar-by-name demo (name a track → pick it → songs like it)
# Render from the REPO ROOT:
#   nix develop .#tui -c vhs tui/assets/similar.tape
# Same prerequisites as demo.tape (engine up on 127.0.0.1:3000 + release build).
# NOTE: the title/artist below must resolve against the catalog — swap them for a
# track you know is present if the pick-list comes up empty.

Output docs/similar.gif

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

# ── menu: let the banner breathe, then → Find Similar Songs (tile 4) ──
Sleep 2500ms
Type "4"
Sleep 1200ms

# ── name the song: title, then Tab to the artist field ──
Type "Midnight City"
Sleep 900ms
Tab
Sleep 500ms
Type "M83"
Sleep 1200ms

# ── resolve the name → catalog candidates (pick-list) ──
Enter
Sleep 3s

# ── glance over the candidate matches before picking ──
Down
Sleep 800ms
Up
Sleep 800ms

# ── pick the track we meant, then find songs like it ──
Type "s"
Sleep 3500ms

# ── walk the similar set ──
Down
Sleep 800ms
Down
Sleep 800ms
Down
Sleep 800ms
Down
Sleep 800ms
Down
Sleep 1500ms

# ── open one to inspect its detail card ──
Enter
Sleep 3s

# ── "more like this" again — chase the thread one hop further ──
Type "s"
Sleep 3s
Down
Sleep 800ms
Down
Sleep 1500ms

Hide
Type "q"
Sleep 300ms
Ctrl+C
